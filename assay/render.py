"""Static company page from a published ticker artifact.

One HTML file per ticker, one inline script for tooltips, no external requests. Every
figure carries a numbered citation that links to the SEC filing it was read from. Charts
are inline SVG.

    python3 -m assay.render --data data --ticker INTC --out INTC.html [--prices prices.json]

The optional prices file maps ticker to a list of [day, close] pairs (split-adjusted,
twelve months); without it the page omits the price chart.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from datetime import date
from html import escape
from pathlib import Path
from typing import Any

from . import animals
from .grading import grade_label

# label, kind, note. Kind picks the number format: pct, pts, x, num, prob.
FACTORS: dict[str, tuple[str, str, str]] = {
    "gross_profitability": ("Gross profitability", "pct", "revenue less cost of goods sold, over total assets"),
    "roic": ("Return on invested capital", "pct", ""),
    "accruals": ("Accruals ratio", "pct", "net income less operating cash flow, over average assets; lower is stronger"),
    "gross_margin_stability_5y": ("Gross margin stability, five years", "pts", "standard deviation of gross margin; lower is stronger"),
    "chs_12m": ("Twelve-month failure probability", "prob", "CHS model; lower is stronger"),
    "altman_z_double_prime": ("Altman Z double-prime", "num", ""),
    "net_debt_to_ebitda": ("Net debt to EBITDA", "x", "lower is stronger"),
    "interest_coverage": ("Interest coverage", "x", ""),
    "current_ratio": ("Current ratio", "x", ""),
    "revenue_cagr_3y": ("Revenue growth, three-year compound", "pct", ""),
    "fcf_cagr_3y": ("Free cash flow growth, three-year compound", "pct", ""),
    "reinvestment_rate": ("Reinvestment rate", "pct", ""),
    "asset_growth_3y": ("Asset growth, three years", "pct", ""),
    "net_share_issuance": ("Net share issuance, one year", "pct", "split-adjusted; lower is stronger"),
}
SLEEVES = (
    ("profitability", "Profitability"),
    ("solvency", "Solvency"),
    ("growth_and_financing", "Growth and financing"),
)
PRICE_METRICS = {
    "momentum_12_1": ("Twelve-month price change, skipping the latest month", "pct", "higher"),
    "distance_from_52_week_high": ("Distance from 52-week high", "pct", "higher"),
    "ebit_to_enterprise_value": ("EBIT to enterprise value", "pct", "lower"),
    "fcf_yield": ("Free cash flow yield", "pct", "lower"),
    "book_to_price": ("Book value to price", "num", "lower"),
    "sales_to_price": ("Sales to price", "num", "lower"),
    "composite_equity_issuance_5y": ("Composite equity issuance, five years", "pct", "higher"),
    "sector_relative_strength": ("Relative strength against the sector, log excess return", "num", "higher"),
}
FLAG_LABELS = {
    "dilution": "Dilution",
    "short_runway": "Short runway",
    "late_filer": "Late filer",
    "thin_data": "Thin data",
    "degenerate_inputs": "Degenerate inputs",
    "distress": "Distress",
    "fortress": "Fortress balance sheet",
}
POLICY_LABELS = {
    "baseline_sector_rollup_equal_sleeves": "sector peers, equal sleeves (the grade)",
    "all_eligible_peer_universe_equal_sleeves": "all graded filers, equal sleeves",
    "same_exchange_peer_universe_equal_sleeves": "same exchange, equal sleeves",
    "sector_rollup_equal_factors": "sector peers, equal inputs",
}
PIOTROSKI_LABELS = {
    "positive_roa": "Return on assets above zero",
    "positive_operating_cash_flow": "Operating cash flow above zero",
    "improving_roa": "Return on assets improved",
    "cash_flow_exceeds_net_income": "Operating cash flow exceeds net income",
    "declining_long_term_leverage": "Long-term leverage fell",
    "improving_current_ratio": "Current ratio improved",
    "no_new_shares": "No new shares issued",
    "improving_gross_margin": "Gross margin improved",
    "improving_asset_turnover": "Asset turnover improved",
}
NAVY = "#000080"


def _piotroski_metric(component: dict[str, Any]) -> str:
    value = component.get("metric")
    if not isinstance(value, (int, float)):
        return ""
    unit = component.get("unit")
    if unit == "USD":
        return _money(value)
    if unit in ("ratio", "growth_rate"):
        return _paren(value * 100, 1, "%")
    if unit == "change_in_ratio":
        return _paren(value * 100, 1, " pts")
    return _paren(value, 2, "")

CSS = """
  html, body { background: #ffffff; color: #000080; margin: 0; padding: 0; }
  body { font-family: Times, "Times New Roman", serif; font-size: 15px; line-height: 1.25; }
  .page { padding-block: 8px; padding-inline: 16px; max-width: 66em; margin: 0 auto; }
  a { color: #0000ee; }
  a:visited { color: #800080; }
  hr { border: 0; border-top: 1px solid #808080; height: 0; margin: 12px 0; }
  h1 { font-size: 24px; font-variant: small-caps; font-weight: bold; text-align: center; margin: 14px 0 2px; letter-spacing: .02em; }
  h2 { font-size: 15px; font-variant: small-caps; font-weight: bold; margin: 22px 0 6px; padding-bottom: 2px; border-bottom: 1px solid #000080; }
  p { margin: 0 0 8px; max-width: 46em; }
  .center { text-align: center; }
  .center p { margin: 0 auto 2px; }
  .small { font-size: 11px; }
  .num { font-variant-numeric: tabular-nums; }
  sup.c { font-size: 9px; line-height: 0; vertical-align: super; margin-left: 1px; white-space: nowrap; }
  sup.c a { text-decoration: none; }
  .scroll { overflow-x: auto; }
  table.data { border-collapse: collapse; margin: 4px 0 6px; font-variant-numeric: tabular-nums; }
  table.data th { text-align: left; font-weight: bold; border-bottom: 1px solid #000080; padding: 3px 10px 3px 0; white-space: nowrap; font-size: 12px; }
  table.data th.n, table.data td.n { text-align: right; }
  table.data td { padding: 3px 10px 3px 0; vertical-align: top; }
  table.data td.n, table.data td.s, table.data td.b, table.data td.date { white-space: nowrap; }
  table.data td.w { white-space: normal; }
  table.data tr.focal td { font-weight: bold; }
  table.data tr.sub td { color: #000080; font-size: 12px; padding-top: 0; }
  table.data tr.total td { border-top: 1px solid #000080; }
  table.nav { width: 100%; border-collapse: collapse; }
  table.nav td { width: 50%; vertical-align: top; padding: 0; }
  table.nav ul { margin: 6px 0; }
  table.nav li { margin-bottom: 8px; }
  table.nav li .small { display: block; }
  .sheet { display: grid; grid-template-columns: minmax(0, 23em) minmax(0, 1fr); gap: 0 28px; align-items: start; margin: 10px 0 6px; }
  @media (max-width: 52em) { .sheet { grid-template-columns: minmax(0, 1fr); } }
  table.score { border-collapse: collapse; width: 100%; border: 1px solid #000080; font-size: 14px; }
  table.score th { background: #e8e8f0; border: 1px solid #000080; padding: 5px 8px; text-align: left; font-weight: bold; }
  table.score th .sub { display: block; font-weight: normal; font-size: 12px; }
  table.score td { border: 1px solid #000080; padding: 4px 8px; vertical-align: top; }
  table.score td.k { font-weight: bold; white-space: nowrap; width: 1%; }
  table.score .big { font-size: 26px; font-weight: bold; line-height: 1; }
  table.score .sub { display: block; font-size: 12px; }
  .pbar { display: inline-block; vertical-align: middle; }
  .lt { display: inline-block; width: 2.6em; }
  table.data td.b { width: 124px; padding-right: 6px; }
  table.data td.s, table.data th.s { font-size: 10px; padding-right: 8px; white-space: nowrap; }
  table.data td.s a { text-decoration: none; }
  .cref { font-size: 10px; white-space: nowrap; }
  .cref a { text-decoration: none; }
  table.data tr.group td { font-weight: bold; font-size: 12px; border-top: 1px solid #000080; padding-top: 6px; }
  table.data tr.reason td { font-size: 11px; padding-top: 0; padding-left: 16px; white-space: normal; }
  table.data td.tag { white-space: normal; word-break: break-all; max-width: 24em; font-size: 12px; }
  table.score td.chartcell { padding: 6px 8px 2px; }
  table.score td.chartcell .caption { margin: 2px 0 4px; }
  .warnband { border: 1px solid #000080; padding: 6px 10px; margin: 8px 0 6px; font-size: 14px; }
  .warnband .item { margin: 2px 0; }
  .ic { width: 13px; height: 13px; vertical-align: -1px; margin-right: 5px; color: #000080; }
  .chart { display: block; width: 100%; height: auto; margin: 2px 0; }
  .chart text { font-family: Times, "Times New Roman", serif; fill: #000080; }
  .caption { font-size: 11px; margin: 0 0 10px; }
  .tip { position: fixed; z-index: 3000; background: #ffffff; color: #000080; border: 1px solid #000080; padding: 4px 7px; font-family: Times, "Times New Roman", serif; font-size: 12px; line-height: 1.3; max-width: 24em; pointer-events: none; }
  .hit { cursor: pointer; }
  .panel { border: 1px solid #000080; padding: 6px 8px; margin: 4px 0 10px; font-size: 12px; line-height: 1.5; }
  .guide { display: none; }
  .fn p { margin: 0 0 3px; font-size: 12px; max-width: 60em; }
  .cols { display: grid; grid-template-columns: minmax(0, 3fr) minmax(0, 2fr); gap: 0 28px; }
  .cols.even { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
  @media (max-width: 52em) { .cols, .cols.even { grid-template-columns: minmax(0, 1fr); } }
  .scroll.peers { overflow: visible; }
  .sheet, .cols, .layout { max-width: 100%; }
  .co { position: relative; }
  .co .card { display: none; position: absolute; left: 0; top: 1.4em; z-index: 1000; max-width: calc(100vw - 32px); width: 20em; background: #ffffff; border: 1px solid #000080; padding: 6px 8px; font-size: 12px; font-weight: normal; line-height: 1.3; white-space: normal; text-align: left; }
  .co:hover, .co:focus-within { z-index: 1001; }
  .side .co .card { left: auto; right: 0; }
  .co:hover .card, .co:focus-within .card { display: block; }
  .co .card .big { font-size: 20px; font-weight: bold; margin-right: 6px; }
  .co .card p { margin: 0 0 2px; max-width: none; }
  .business { margin: 6px 0 10px; max-width: 60em; }
  .business p { max-width: 60em; }
  .footer p { max-width: none; }
  .top { font-size: 10px; font-variant: normal; font-weight: normal; margin-left: 8px; }
  .bar { background: #000080; color: #ffffff; padding: 5px 8px; margin: 8px 0 10px; display: flex; flex-wrap: wrap; justify-content: space-between; gap: 3px 10px; align-items: center; font-size: 13px; font-variant: small-caps; white-space: nowrap; }
  .bar a { color: #ffffff; text-decoration: none; }
  .bar a:hover { text-decoration: underline; }
  .bar .menu { position: relative; }
  .bar .menu > a::after { content: "\\00a0\\25BE"; font-size: 9px; }
  .bar .drop { display: none; position: absolute; left: -8px; top: 100%; padding: 6px 0; background: #000080; border: 1px solid #ffffff; border-top: 0; z-index: 1002; min-width: 12em; }
  .bar .menu:hover .drop, .bar .menu:focus-within .drop { display: block; }
  .bar .drop a { display: block; padding: 3px 12px; }
  .bar .drop a:hover { background: #ffffff; color: #000080; text-decoration: none; }
  .bar form { font-variant: normal; display: flex; gap: 3px; }
  .sitemap { display: flex; flex-wrap: wrap; gap: 8px 28px; font-size: 12px; margin: 10px 0 8px; }
  .sitemap b { font-variant: small-caps; letter-spacing: .04em; }
  .credits summary { cursor: pointer; }
  .credits p { margin: 2px 0 0; }
  .ranges { display: flex; flex-wrap: wrap; gap: 4px 6px; align-items: center; margin: 0 0 4px; font-size: 12px; }
  .ranges button { font-family: Times, "Times New Roman", serif; font-size: 11.5px; padding: 0 6px; border: 1px solid #000080; background: #ffffff; color: #000080; cursor: pointer; }
  .ranges button:hover { background: #e8e8f0; }
  svg[data-prices] { cursor: grab; touch-action: none; }
  .lolli { cursor: pointer; }
  .bar input { font-family: Times, "Times New Roman", serif; font-size: 12px; width: 5em; padding: 1px 3px; border: 1px solid #ffffff; text-transform: uppercase; }
  .bar button { font-family: Times, "Times New Roman", serif; font-size: 12px; padding: 1px 6px; border: 1px solid #ffffff; background: #ffffff; color: #000080; }
  .hero { display: block; width: 100%; height: 150px; object-fit: cover; object-position: 50% 55%; border: 1px solid #000080; margin: 4px 0 10px; }
"""

WARN_SYMBOL = """<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <symbol id="warn" viewBox="0 0 16 16">
    <path d="M8 1.6 L15 14 H1 Z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>
    <path d="M8 6 V10" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
    <circle cx="8" cy="12.2" r="0.9" fill="currentColor"/>
  </symbol>
</svg>"""


NAV = [
    ("index.html", "Home", ()),
    ("companies.html", "Companies", ()),
    ("sectors.html", "Sectors", ()),
    ("calendar.html", "Calendar", ()),
    ("last-night.html", "Last night", ()),
    ("warnings.html", "Screens", (
        ("warnings.html", "Warnings"), ("price-and-condition.html", "Price and condition"), ("then-and-now.html", "Then and now"),
        ("size-bands.html", "Size bands"), ("ceased.html", "Ceased filers"))),
    ("time-machine.html", "Tools", (
        ("time-machine.html", "Time machine"), ("as-of.html", "Point in time"), ("data.html", "Data exports"),
        ("runs.html", "Run log"), ("forecasts.html", "Forecast log"),
        ("https://www.sec.gov/edgar/searchedgar/companysearch", "EDGAR company search"))),
    ("methodology.html", "Method", (
        ("methodology.html", "Method"), ("coverage.html", "Coverage"), ("about.html", "About"))),
]


def _nav_link(base: str, path: str, label: str) -> str:
    href = path if path.startswith("http") else base + path
    return f'<a href="{href}">{label}</a>'


CHARSET = '<meta charset="utf-8">'


REPO_URL = "https://github.com/ekruges/assay"
SITE_URL = "https://ezrakruger.cc"


def site_map(page_link: str = "./") -> str:
    """Every page of the site in its menu groups, for the foot of every page, with the code and the author's site."""
    base = escape(page_link or "./")
    groups = ['<div><b>Pages</b><br>' + " &middot; ".join(_nav_link(base, path, label) for path, label, children in NAV if not children) + "</div>"]
    for path, label, children in NAV:
        if children:
            groups.append(f"<div><b>{escape(label)}</b><br>" + " &middot; ".join(_nav_link(base, child_path, child_label) for child_path, child_label in children) + "</div>")
    groups.append(f'<div><b>Source</b><br><a href="{REPO_URL}">GitHub</a> &middot; <a href="{SITE_URL}">ezrakruger.cc</a></div>')
    return '<div class="sitemap">' + "".join(groups) + "</div>"


def credit_footer() -> str:
    """Photograph credits, collapsed behind one line; every page that shows a photograph carries it."""
    lines = "; ".join(f'{escape(c["name"])}: <a href="{escape(c["source"])}">{escape(c["author"] or "unknown")}, {escape(c["license"])}</a>' for c in animals.credits())
    return f'<details class="small credits"><summary>Photographs from Wikimedia Commons</summary><p>{lines}.</p></details>' if lines else ""


def nav_bar(page_link: str = "./") -> str:
    """One row of links; a group opens its pages as a hover menu and its own link goes to the group's first page."""
    base = escape(page_link or "./")
    links = ""
    for path, label, children in NAV:
        if children:
            drop = "".join(_nav_link(base, child_path, child_label) for child_path, child_label in children)
            links += f'<div class="menu">{_nav_link(base, path, label)}<div class="drop">{drop}</div></div>'
        else:
            links += _nav_link(base, path, label)
    return (
        f'<div class="bar">{links}'
        f'<form onsubmit="var t=this.t.value.trim().toUpperCase(); if(t){{location.href=\'{base}\'+t+\'.html\'}} return false">'
        '<input id="t" name="t" placeholder="TICKER" maxlength="8" autocomplete="off" aria-label="ticker"><button type="submit">Go</button></form></div>'
    )


TIP_SCRIPT = """<script>
(function(){
  var tip=document.createElement('div');tip.className='tip';tip.hidden=true;document.body.appendChild(tip);
  function show(text,e){tip.textContent='';String(text).split('|').forEach(function(l,i){if(i)tip.appendChild(document.createElement('br'));tip.appendChild(document.createTextNode(l));});tip.hidden=false;move(e);}
  function move(e){var x=e.clientX+14,y=e.clientY+14,r=tip.getBoundingClientRect();if(x+r.width>window.innerWidth-8)x=e.clientX-r.width-14;if(y+r.height>window.innerHeight-8)y=e.clientY-r.height-14;tip.style.left=x+'px';tip.style.top=y+'px';}
  function hide(){tip.hidden=true;}
  function target(e,sel){return e.target&&e.target.closest?e.target.closest(sel):null;}
  document.addEventListener('mousemove',function(e){var t=target(e,'[data-tip]');if(t){if(tip.hidden||tip.getAttribute('data-for')!==t.getAttribute('data-tip')){tip.setAttribute('data-for',t.getAttribute('data-tip'));show(t.getAttribute('data-tip'),e);}else{move(e);}}else if(!tip.hidden&&!target(e,'svg[data-series]')){hide();}});
  document.addEventListener('click',function(e){var h=target(e,'[data-href]');if(h){location.href=h.getAttribute('data-href');return;}var l=target(e,'[data-list]');if(!l)return;var svg=l.closest('svg');var panel=svg.nextElementSibling&&svg.nextElementSibling.className==='panel'?svg.nextElementSibling:null;if(!panel){panel=document.createElement('div');panel.className='panel';svg.parentNode.insertBefore(panel,svg.nextSibling);}var page=document.querySelector('.page');var base=(page&&page.getAttribute('data-base'))||'./';var items=l.getAttribute('data-list').split(',').filter(Boolean);panel.textContent='';var b=document.createElement('b');b.textContent=(l.getAttribute('data-title')||'')+' ('+items.length+'): ';panel.appendChild(b);items.forEach(function(t,i){if(i)panel.appendChild(document.createTextNode(', '));var a=document.createElement('a');a.href=base+t+'.html';a.textContent=t;panel.appendChild(a);});});
  var q=document.querySelector('[data-quote]');
  if(q&&window.fetch){var page=document.querySelector('.page');var base=(page&&page.getAttribute('data-base'))||'./';
    fetch(base+'api/quote?ticker='+encodeURIComponent(q.getAttribute('data-quote')),{headers:{Accept:'application/json'}}).then(function(r){return r.ok?r.json():null;}).then(function(d){
      if(!d||d.status!=='resolved'||!d.price)return;
      var p=q.querySelector('.quote-price'),s=q.querySelector('.quote-sub'),l=document.querySelector('[data-quote-label]');
      p.textContent='$'+Number(d.price).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
      var when=d.price_timestamp?new Date(d.price_timestamp):null;var t=when?when.toLocaleTimeString([],{hour:'numeric',minute:'2-digit'}):'';
      var chg=(typeof d.day_change_percent==='number')?((d.day_change_percent>=0?'+':'')+(d.day_change_percent*100).toFixed(2)+'% on the day; '):'';
      s.textContent='live, '+t+' '+(d.feed||'')+' feed; '+chg+'refreshes every 15 seconds; never enters the grade';
      if(l)l.textContent='Price, live';
    }).catch(function(){});
  }
  Array.prototype.forEach.call(document.querySelectorAll('svg[data-series]'),function(svg){
    var raw=null,pts=[];
    function points(){var a=svg.getAttribute('data-series');if(a!==raw){raw=a;pts=a.split(';').map(function(s){var i=s.indexOf(':');return [parseFloat(s.slice(0,i)),s.slice(i+1)];});}return pts;}
    svg.addEventListener('mousemove',function(e){if(target(e,'.lolli')||svg.getAttribute('data-dragging'))return;var ps=points();var guide=svg.querySelector('.guide');var pt=svg.createSVGPoint();pt.x=e.clientX;pt.y=e.clientY;var loc=pt.matrixTransform(svg.getScreenCTM().inverse());var best=ps[0];for(var i=1;i<ps.length;i++){if(Math.abs(ps[i][0]-loc.x)<Math.abs(best[0]-loc.x))best=ps[i];}if(guide){guide.setAttribute('x1',best[0]);guide.setAttribute('x2',best[0]);guide.style.display='block';}tip.setAttribute('data-for','series');show(best[1],e);});
    svg.addEventListener('mouseleave',function(){var guide=svg.querySelector('.guide');if(guide)guide.style.display='none';hide();});
  });
  var pc=document.querySelector('svg[data-prices]');
  if(pc&&window.fetch){
    var MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    function longDate(d){return MON[+d.slice(5,7)-1]+' '+(+d.slice(8,10))+', '+d.slice(0,4);}
    function fmt(v){return v>=1000?Math.round(v).toLocaleString():v>=100?v.toFixed(0):v>=10?v.toFixed(1):v.toFixed(2);}
    function nice(raw){if(raw<=0)return 1;var mag=Math.pow(10,Math.floor(Math.log10(raw)));var ms=[1,2,5,10];for(var i=0;i<ms.length;i++)if(ms[i]*mag>=raw)return ms[i]*mag;return 10*mag;}
    function esc(x){return String(x==null?'':x).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
    var asof=pc.getAttribute('data-asof'),filed=(pc.getAttribute('data-filed')||'').split(',').filter(Boolean);
    Promise.all([fetch(pc.getAttribute('data-prices')).then(function(r){return r.ok?r.json():null;}).catch(function(){return null;}),fetch(pc.getAttribute('data-rescans')).then(function(r){return r.ok?r.text():null;}).catch(function(){return null;})]).then(function(res){
      var P=res[0];if(!P||!P.d||P.d.length<20)return;var days=P.d,closes=P.c,n=days.length;
      var rescans=[];if(res[1])res[1].split(/\\r?\\n/).slice(1).forEach(function(l){if(!l)return;var c=l.split(',');rescans.push({d:c[0],g:c[2]||null,p:c[3]===''||c[3]==null?null:parseFloat(c[3])});});
      var b=n-1,a=Math.max(0,n-253),w=480,h=170,L=40,R=46,T=22,B=22;
      var bar=document.createElement('div');bar.className='ranges';
      [['1M',22],['3M',66],['6M',130],['1Y',253],['2Y',505],['5Y',1260],['10Y',2520],['All',n]].forEach(function(r){var btn=document.createElement('button');btn.type='button';btn.textContent=r[0];btn.addEventListener('click',function(){b=n-1;a=Math.max(0,b-r[1]+1);draw();});bar.appendChild(btn);});
      var lbl=document.createElement('span');lbl.className='small';bar.appendChild(lbl);pc.parentNode.insertBefore(bar,pc);
      function idxFor(day){var lo=0,hi=n-1;while(lo<hi){var m=(lo+hi)>>1;if(days[m]<day)lo=m+1;else hi=m;}return lo;}
      function draw(){var m=b-a+1,seg=closes.slice(a,b+1),lo=Math.min.apply(null,seg),hi=Math.max.apply(null,seg),pad=(hi-lo)*0.08||1,y0=lo-pad,y1=hi+pad,i;
        function X(k){return L+(k-a)/Math.max(m-1,1)*(w-L-R);}function Y(v){return T+(y1-v)/(y1-y0)*(h-T-B);}
        var path='',area='M'+X(a).toFixed(1)+' '+Y(y0).toFixed(1);for(i=a;i<=b;i++){var pt=X(i).toFixed(1)+' '+Y(closes[i]).toFixed(1);path+=(i>a?'L':'M')+pt;area+='L'+pt;}area+='L'+X(b).toFixed(1)+' '+Y(y0).toFixed(1)+'Z';
        var step=nice((y1-y0)/3),v=(Math.floor(y0/step)+1)*step,grid='';while(v<y1){grid+='<line x1="'+L+'" y1="'+Y(v).toFixed(1)+'" x2="'+(w-R)+'" y2="'+Y(v).toFixed(1)+'" stroke="#c8c8d8"/><text x="'+(L-5)+'" y="'+(Y(v)+3.5).toFixed(1)+'" font-size="10" text-anchor="end">$'+fmt(v)+'</text>';v+=step;}
        var labels='',seen={},every=Math.max(1,Math.ceil(m/8));
        for(i=a;i<=b;i++){var d=days[i],txt=null;
          if(m<=70){if((i-a)%every===0&&i>a&&i<b-1)txt=MON[+d.slice(5,7)-1]+' '+(+d.slice(8,10));}
          else if(m<=300){if(!seen[d.slice(0,7)]){seen[d.slice(0,7)]=1;if(i>a&&i<b-5)txt=MON[+d.slice(5,7)-1];}}
          else if(m<=1300){if(!seen[d.slice(0,7)]){seen[d.slice(0,7)]=1;var mo=d.slice(5,7);if(i>a&&i<b-5&&(mo==='01'||mo==='04'||mo==='07'||mo==='10'))txt=mo==='01'?d.slice(0,4):MON[+mo-1];}}
          else{if(!seen[d.slice(0,4)]){seen[d.slice(0,4)]=1;if(i>a&&i<b-20)txt=d.slice(0,4);}}
          if(txt)labels+='<text x="'+X(i).toFixed(1)+'" y="'+(h-B+13)+'" font-size="10" text-anchor="middle">'+esc(txt)+'</text>';}
        var ticks='';filed.forEach(function(d){if(d<days[a]||d>days[b])return;var x=X(idxFor(d)).toFixed(1);ticks+='<line x1="'+x+'" y1="'+Y(y0).toFixed(1)+'" x2="'+x+'" y2="'+(Y(y0)+6).toFixed(1)+'" stroke="#000080"/>';});
        var inWin=rescans.filter(function(r){return r.d>=days[a]&&r.d<=days[b];}),xs=inWin.map(function(r){return X(idxFor(r.d));}),marks='',crowded=false,q;
        for(q=1;q<xs.length;q++)if(xs[q]-xs[q-1]<13){crowded=true;break;}
        var changes=[];inWin.forEach(function(r,k){if(k===0||r.g!==inWin[k-1].g)changes.push(k);});
        var heads={};if(!crowded){inWin.forEach(function(r,k){heads[k]=1;});}else{changes.forEach(function(k){var ok=true;for(q=0;q<changes.length;q++)if(changes[q]!==k&&Math.abs(xs[changes[q]]-xs[k])<13){ok=false;break;}if(ok)heads[k]=1;});}
        inWin.forEach(function(r,k){var x=xs[k],y=Y(closes[idxFor(r.d)]),room=!!heads[k];
          var tip=longDate(r.d)+': '+(r.g||'no letter')+(r.p==null?'':', percentile '+(r.p*100).toFixed(1))+'; click to open the rescan';
          if(room&&r.g)marks+='<g class="lolli" data-tip="'+esc(tip)+'" data-href="'+esc(asof+r.d)+'"><line x1="'+x.toFixed(1)+'" y1="'+y.toFixed(1)+'" x2="'+x.toFixed(1)+'" y2="'+(y-16).toFixed(1)+'" stroke="#000080"/><circle cx="'+x.toFixed(1)+'" cy="'+(y-22).toFixed(1)+'" r="6.5" fill="#ffffff" stroke="#000080"/><text x="'+x.toFixed(1)+'" y="'+(y-19.2).toFixed(1)+'" font-size="'+(r.g.length>1?7:9)+'" font-weight="bold" text-anchor="middle">'+esc(r.g)+'</text></g>';
          else marks+='<g class="lolli" data-tip="'+esc(tip)+'" data-href="'+esc(asof+r.d)+'"><line x1="'+x.toFixed(1)+'" y1="'+y.toFixed(1)+'" x2="'+x.toFixed(1)+'" y2="'+(y-10).toFixed(1)+'" stroke="#000080"/><circle cx="'+x.toFixed(1)+'" cy="'+(y-12).toFixed(1)+'" r="2" fill="#000080"/></g>';});
        var last=closes[b],series=[];for(i=a;i<=b;i++){var cur=null;for(var q=0;q<rescans.length;q++){if(rescans[q].d<=days[i]&&rescans[q].g)cur=rescans[q];}series.push(X(i).toFixed(1)+':'+longDate(days[i])+'|$'+closes[i].toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})+'|'+(cur?cur.g+' since '+longDate(cur.d):'no letter yet'));}
        pc.setAttribute('data-series',series.join(';'));
        pc.innerHTML=grid+'<path d="'+area+'" fill="#e8e8f0"/><path d="'+path+'" fill="none" stroke="#000080" stroke-width="1.3"/><line x1="'+L+'" y1="'+Y(y0).toFixed(1)+'" x2="'+(w-R)+'" y2="'+Y(y0).toFixed(1)+'" stroke="#000080"/>'+ticks+labels+'<circle cx="'+X(b).toFixed(1)+'" cy="'+Y(last).toFixed(1)+'" r="3" fill="#ffffff" stroke="#000080"/><text x="'+(X(b)+6).toFixed(1)+'" y="'+(Y(last)+3.5).toFixed(1)+'" font-size="10" font-weight="bold">$'+fmt(last)+'</text><line class="guide" x1="0" y1="'+T+'" x2="0" y2="'+Y(y0).toFixed(1)+'" stroke="#000080" stroke-dasharray="2 2" style="display:none"/>'+marks;
        lbl.textContent=longDate(days[a])+' to '+longDate(days[b])+', '+inWin.length+' rescans; drag to scroll';}
      var drag=null;
      pc.addEventListener('pointerdown',function(e){drag={x:e.clientX,a:a,b:b};pc.setAttribute('data-dragging','1');pc.setPointerCapture(e.pointerId);});
      pc.addEventListener('pointermove',function(e){if(!drag)return;var rect=pc.getBoundingClientRect();var per=(w-L-R)/(drag.b-drag.a)*(rect.width/w);var shift=Math.round((drag.x-e.clientX)/per);var span=drag.b-drag.a;var na=Math.max(0,Math.min(n-1-span,drag.a+shift));if(na!==a){a=na;b=na+span;draw();}});
      function endDrag(){drag=null;pc.removeAttribute('data-dragging');}
      pc.addEventListener('pointerup',endDrag);pc.addEventListener('pointercancel',endDrag);
      draw();});
  }
})();
</script>"""


class Citations:
    """Numbered receipts in order of first use. One number per filing, tag and period."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.index: dict[tuple[str, str, str], int] = {}

    def cite(self, receipts: list[dict[str, Any]] | None, inline: bool = False) -> str:
        numbers: list[int] = []
        for receipt in receipts or []:
            if not receipt or not receipt.get("filing_url"):
                continue
            key = (str(receipt.get("accession")), str(receipt.get("tag")), str(receipt.get("end")))
            number = self.index.get(key)
            if number is None:
                number = len(self.items) + 1
                self.index[key] = number
                self.items.append(receipt)
            if number not in numbers:
                numbers.append(number)
        if not numbers:
            return ""
        if inline:
            return f'<span class="cref">[{self._links(numbers)}]</span>'
        return f'<sup class="c">[{self._links(numbers)}]</sup>'

    def _links(self, numbers: list[int]) -> str:
        """Consecutive numbers collapse to a range. A range from one filing links to it; a mixed range links to the sources table."""
        runs: list[list[int]] = []
        for n in sorted(numbers):
            if runs and n == runs[-1][-1] + 1:
                runs[-1].append(n)
            else:
                runs.append([n])
        parts = []
        for run in runs:
            receipts = [self.items[n - 1] for n in run]
            if len(run) == 1:
                parts.append(f'<a href="{escape(receipts[0]["filing_url"])}" title="{escape(_receipt_title(receipts[0]))}">{run[0]}</a>')
                continue
            urls = {r["filing_url"] for r in receipts}
            href = next(iter(urls)) if len(urls) == 1 else f"#src-{run[0]}"
            title = f"receipts {run[0]} to {run[-1]}" + (", one filing" if len(urls) == 1 else f", {len(urls)} filings; opens the sources table")
            parts.append(f'<a href="{escape(href)}" title="{escape(title)}">{run[0]}-{run[-1]}</a>')
        return ",".join(parts)


def _receipt_title(receipt: dict[str, Any]) -> str:
    return (
        f"{receipt.get('namespace')}:{receipt.get('tag')} = {receipt.get('value')} "
        f"{receipt.get('unit') or ''}; period end {receipt.get('end')}; {receipt.get('form')} filed {receipt.get('filed')}"
    )


def fmt(value: Any, kind: str) -> str:
    if value is None or isinstance(value, str):
        return escape(str(value)) if value is not None else "n/a"
    if kind == "pct":
        return _paren(value * 100, 2 if 0 < abs(value * 100) < 0.1 else 1, "%")
    if kind == "prob":
        return f"{value * 100:.2f}%"
    if kind == "pts":
        return f"{value * 100:.1f} pts"
    if kind == "x":
        return _paren(value, 2, "x")
    if kind == "money":
        return _money(value)
    if kind == "int":
        return f"{value:,.0f}"
    return _paren(value, 2, "")


def _paren(value: float, digits: int, suffix: str) -> str:
    text = f"{abs(value):,.{digits}f}{suffix}"
    return f"({text})" if value < 0 else text


def _money(value: float) -> str:
    sign = "-" if value < 0 else ""
    value = abs(value)
    for unit, name in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if value >= unit:
            return f"{sign}${value / unit:,.1f}{name}"
    return f"{sign}${value:,.0f}"


def pct_points(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}"


def long_date(text: str | None) -> str:
    if not text:
        return "n/a"
    return date.fromisoformat(text).strftime("%b %-d, %Y")


def bar(percentile: float | None, width: int = 120, tip: str | None = None) -> str:
    """Hairline track from 0 to 100 with a navy marker; lower is stronger, so left is better."""
    if percentile is None:
        return ""
    x = round(2 + percentile * (width - 4), 1)
    attr = f' data-tip="{escape(tip)}"' if tip else f' data-tip="percentile {percentile * 100:.1f}; lower is stronger"'
    return (
        f'<svg class="pbar" width="{width}" height="10" viewBox="0 0 {width} 10" aria-label="percentile {percentile * 100:.0f}"{attr}>'
        f'<line x1="2" y1="5" x2="{width - 2}" y2="5" stroke="#c8c8d8" stroke-width="1"/>'
        + "".join(f'<line x1="{2 + (width - 4) * f:.1f}" y1="3" x2="{2 + (width - 4) * f:.1f}" y2="7" stroke="#c8c8d8" stroke-width="1"/>' for f in (0.2, 0.4, 0.6, 0.8))
        + f'<rect x="{x - 1.5}" y="1" width="3" height="8" fill="{NAVY}"/></svg>'
    )


def ruler(percentile: float) -> str:
    w, h = 200, 28
    ticks = "".join(f'<line x1="{6 + 188 * f:.1f}" y1="10" x2="{6 + 188 * f:.1f}" y2="18" stroke="{NAVY}" stroke-width="1"/>' for f in (0, .2, .4, .6, .8, 1))
    labels = "".join(f'<text x="{6 + 188 * (f + .1):.1f}" y="27" font-size="10" text-anchor="middle">{g}</text>' for f, g in zip((0, .2, .4, .6, .8), "ABCDE"))
    x = 6 + 188 * percentile
    return (
        f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" aria-hidden="true" style="display:block;margin:4px 0 2px">'
        f'<line x1="6" y1="14" x2="194" y2="14" stroke="{NAVY}" stroke-width="1"/>{ticks}'
        f'<polygon points="{x - 4:.1f},3 {x + 4:.1f},3 {x:.1f},11" fill="{NAVY}"/>'
        f'<g font-family="Times, serif" fill="{NAVY}">{labels}</g></svg>'
    )


def lollipops(rescans: list[tuple[str, str | None, float | None]], idx_for, x_at, y_for, ticker: str, base: str) -> str:
    """A stem at every rescan with a lettered head where there is room; when rescans crowd, only the rescans where the letter changed keep a head."""
    marks = ""
    xs = [x_at(idx_for(d)) for d, _, _ in rescans]
    crowded = any(abs(xs[k] - xs[k - 1]) < 13 for k in range(1, len(xs)))
    changes = [k for k in range(len(rescans)) if k == 0 or rescans[k][1] != rescans[k - 1][1]]
    heads = set(range(len(rescans))) if not crowded else {k for k in changes if all(abs(xs[k] - xs[m]) >= 13 for m in changes if m != k)}
    for k, ((d, g, pct), x) in enumerate(zip(rescans, xs)):
        i = idx_for(d)
        y = y_for(i)
        room = k in heads
        tip = f"{long_date(d)}: {g or 'no letter'}" + (f", percentile {pct * 100:.1f}" if pct is not None else "") + "; click to open the rescan"
        href = f' data-href="{base}as-of.html#{escape(ticker)}~{escape(d)}"' if ticker else ""
        if room and g:
            marks += (f'<g class="lolli" data-tip="{escape(tip)}"{href}><line x1="{x:.1f}" y1="{y:.1f}" x2="{x:.1f}" y2="{y - 16:.1f}" stroke="{NAVY}" stroke-width="1"/>'
                      f'<circle cx="{x:.1f}" cy="{y - 22:.1f}" r="6.5" fill="#ffffff" stroke="{NAVY}" stroke-width="1"/>'
                      f'<text x="{x:.1f}" y="{y - 19.2:.1f}" font-size="{7 if len(g) > 1 else 9}" font-weight="bold" text-anchor="middle">{escape(g)}</text></g>')
        else:
            marks += (f'<g class="lolli" data-tip="{escape(tip)}"{href}><line x1="{x:.1f}" y1="{y:.1f}" x2="{x:.1f}" y2="{y - 10:.1f}" stroke="{NAVY}" stroke-width="1"/>'
                      f'<circle cx="{x:.1f}" cy="{y - 12:.1f}" r="2" fill="{NAVY}"/></g>')
    return marks


def price_chart(prices: list[list[Any]], history: list[dict[str, Any]], filed_dates: list[str],
                rescans: list[tuple[str, str | None, float | None]] | None = None, ticker: str = "", base: str = "./") -> str:
    """Twelve months of closes with a lollipop at every rescan in the window carrying the letter held then. The SVG
    carries the paths of the full price file and the rescan CSV, and the page script turns it into the scrollable chart."""
    if len(prices) < 20:
        return ""
    w, h, left, right, top, bottom = 480, 170, 40, 46, 22, 22
    days = [p[0] for p in prices]
    closes = [float(p[1]) for p in prices]
    lo, hi = min(closes), max(closes)
    pad = (hi - lo) * 0.08 or 1
    y0, y1 = lo - pad, hi + pad
    n = len(closes)

    def x_at(i: int) -> float:
        return left + i / (n - 1) * (w - left - right)

    def y_at(v: float) -> float:
        return top + (y1 - v) / (y1 - y0) * (h - top - bottom)

    def idx_for(day: str) -> int:
        for i, d in enumerate(days):
            if d >= day:
                return i
        return n - 1

    path = "M" + "L".join(f"{x_at(i):.1f} {y_at(v):.1f}" for i, v in enumerate(closes))
    area = f"M{x_at(0):.1f} {y_at(y0):.1f}L" + "L".join(f"{x_at(i):.1f} {y_at(v):.1f}" for i, v in enumerate(closes)) + f"L{x_at(n - 1):.1f} {y_at(y0):.1f}Z"
    grid = ""
    step = _nice_step((y1 - y0) / 3)
    v = (int(y0 / step) + 1) * step
    while v < y1:
        grid += f'<line x1="{left}" y1="{y_at(v):.1f}" x2="{w - right}" y2="{y_at(v):.1f}" stroke="#c8c8d8" stroke-width="1"/>'
        grid += f'<text x="{left - 5}" y="{y_at(v) + 3.5:.1f}" font-size="10" text-anchor="end">${v:g}</text>'
        v += step
    months = ""
    seen: set[str] = set()
    for i, d in enumerate(days):
        m = d[:7]
        if m not in seen and (i == 0 or d[8:10] <= "03"):
            seen.add(m)
            if i > 0 and i < n - 5:
                months += f'<text x="{x_at(i):.1f}" y="{h - bottom + 13}" font-size="10" text-anchor="middle">{date.fromisoformat(d).strftime("%b")}</text>'
    ticks = "".join(
        f'<line x1="{x_at(idx_for(d)):.1f}" y1="{y_at(y0):.1f}" x2="{x_at(idx_for(d)):.1f}" y2="{y_at(y0) + 6:.1f}" stroke="{NAVY}" stroke-width="1"/>'
        for d in sorted(set(filed_dates)) if days[0] <= d <= days[-1]
    )
    points = sorted((p for p in history if p.get("grade")), key=lambda p: p["date"])
    marks = lollipops([(d, g, pct) for d, g, pct in (rescans or []) if days[0] <= d <= days[-1]], idx_for, x_at, lambda i: y_at(closes[i]), ticker, base)

    def letter_on(day: str) -> str:
        current = None
        for point in points:
            if point["date"] <= day:
                current = point
        return f'{current["grade"]} since {long_date(current["date"])}' if current else "no letter yet"

    series = ";".join(f'{x_at(i):.1f}:{long_date(d)}|${c:,.2f}|{letter_on(d)}' for i, (d, c) in enumerate(zip(days, closes)))
    attrs = (f' data-prices="{base}prices/{escape(ticker)}.json" data-rescans="{base}downloads/{escape(ticker)}-rescans.csv" data-asof="{base}as-of.html#{escape(ticker)}~"'
             f' data-filed="{escape(",".join(sorted(set(filed_dates))))}"') if ticker else ""
    last_x, last_y = x_at(n - 1), y_at(closes[-1])
    return (
        f'<svg class="chart" viewBox="0 0 {w} {h}" data-series="{escape(series)}"{attrs} aria-label="Split-adjusted close over twelve months with the letter grade at each rescan that set it">'
        f"{grid}"
        f'<line x1="{left}" y1="{y_at(y0):.1f}" x2="{w - right}" y2="{y_at(y0):.1f}" stroke="{NAVY}" stroke-width="1"/>'
        f'<path d="{area}" fill="{NAVY}" fill-opacity="0.035"/>'
        f'<path d="{path}" fill="none" stroke="{NAVY}" stroke-width="1.3"/>'
        f"{months}{ticks}{marks}"
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="3.5" fill="#ffffff" stroke="{NAVY}" stroke-width="1.3"/>'
        f'<text x="{last_x + 7:.1f}" y="{last_y + 4:.1f}" font-size="10" font-weight="bold">${closes[-1]:,.2f}</text>'
        f'<line class="guide" x1="0" y1="{top}" x2="0" y2="{y_at(y0):.1f}" stroke="{NAVY}" stroke-width="1" stroke-dasharray="2 2"/>'
        f'<rect x="{left}" y="{top}" width="{w - left - right}" height="{h - top - bottom}" fill="transparent"/>'
        "</svg>"
    )


def rescan_chart(rescans: list[tuple[str, str | None, float | None]]) -> str:
    """Percentile at every rescan on file, drawn as a step line with band edges; hover reads the date, letter and percentile."""
    w, h, left, right, top, bottom = 480, 120, 30, 10, 10, 18
    n = len(rescans)

    def x_at(i: int) -> float:
        return left + i / max(n - 1, 1) * (w - left - right)

    def y_at(p: float) -> float:
        return top + p * (h - top - bottom)

    bands = "".join(f'<line x1="{left}" y1="{y_at(q):.1f}" x2="{w - right}" y2="{y_at(q):.1f}" stroke="#c8c8d8"/>' for q in (0.2, 0.4, 0.6, 0.8))
    letters = "".join(f'<text x="{left - 6}" y="{y_at(q + 0.1) + 3:.1f}" font-size="9" font-weight="bold" text-anchor="end">{g}</text>' for q, g in zip((0, 0.2, 0.4, 0.6, 0.8), "ABCDE"))
    segments, current = [], []
    for i, (_, _, pct) in enumerate(rescans):
        if pct is None:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(f"{x_at(i):.1f} {y_at(pct):.1f}")
    if current:
        segments.append(current)
    paths = "".join(f'<path d="M{"L".join(seg)}" fill="none" stroke="#000080" stroke-width="1.2"/>' if len(seg) > 1 else f'<circle cx="{seg[0].split()[0]}" cy="{seg[0].split()[1]}" r="1.5" fill="#000080"/>' for seg in segments)
    years = ""
    seen: set[str] = set()
    for i, (day, _, _) in enumerate(rescans):
        if day[:4] not in seen:
            seen.add(day[:4])
            if i > 0:
                years += f'<text x="{x_at(i):.1f}" y="{h - 4}" font-size="9" text-anchor="middle">{day[:4]}</text>'
    series = ";".join(f"{x_at(i):.1f}:{long_date(day)}|{(letter or 'no letter')}" + (f", percentile {pct * 100:.1f}" if pct is not None else "") for i, (day, letter, pct) in enumerate(rescans))
    return (f'<svg class="chart" viewBox="0 0 {w} {h}" data-series="{escape(series)}" aria-label="percentile at every rescan">{bands}{letters}'
            f'<line x1="{left}" y1="{y_at(1):.1f}" x2="{w - right}" y2="{y_at(1):.1f}" stroke="#000080"/>{paths}{years}'
            f'<line class="guide" x1="0" y1="{top}" x2="0" y2="{y_at(1):.1f}" stroke="#000080" stroke-dasharray="2 2"/>'
            f'<rect x="{left}" y="{top}" width="{w - left - right}" height="{h - top - bottom}" fill="transparent"/></svg>')


def _nice_step(raw: float) -> float:
    if raw <= 0:
        return 1
    magnitude = 10 ** int(f"{raw:e}".split("e")[1])
    for m in (1, 2, 5, 10):
        if m * magnitude >= raw:
            return m * magnitude
    return 10 * magnitude


def density_chart(values: list[float], focal: float | None, focal_label: str, named: list[tuple[str, float, str]], points: list[tuple[float, str, str]] | None = None) -> str:
    """Kernel density of peer composite scores with the five letter bands, the company, and a few named peers.
    `points` gives every peer as (composite, tooltip, href) for hover marks along the curve."""
    if len(values) < 10:
        return ""
    w, h, left, right, top, bottom = 640, 170, 30, 30, 46, 24
    xs = sorted(values)
    n = len(xs)
    sd = statistics.pstdev(xs) or 1e-6
    bw = 1.06 * sd * n ** (-0.2)
    lo, hi = xs[0] - 2 * bw, xs[-1] + 2 * bw
    span = hi - lo

    def x_at(v: float) -> float:
        return left + (v - lo) / span * (w - left - right)

    def density(v: float) -> float:
        return sum(math.exp(-0.5 * ((v - x) / bw) ** 2) for x in xs) / (n * bw * math.sqrt(2 * math.pi))

    steps = 160
    grid = [lo + span * i / steps for i in range(steps + 1)]
    dens = [density(v) for v in grid]
    peak = max(dens) or 1

    def y_at(d: float) -> float:
        return h - bottom - d / peak * (h - top - bottom)

    line = "M" + "L".join(f"{x_at(v):.1f} {y_at(d):.1f}" for v, d in zip(grid, dens))
    area = f"M{x_at(lo):.1f} {h - bottom}L" + "L".join(f"{x_at(v):.1f} {y_at(d):.1f}" for v, d in zip(grid, dens)) + f"L{x_at(hi):.1f} {h - bottom}Z"
    edges = [xs[min(n - 1, int(round(q * (n - 1))))] for q in (0.2, 0.4, 0.6, 0.8)]
    bands = ""
    bounds = [lo, *edges, hi]
    for i, letter in enumerate("ABCDE"):
        mid = x_at((bounds[i] + bounds[i + 1]) / 2)
        bands += f'<text x="{mid:.1f}" y="{h - bottom - 4}" font-size="11" font-weight="bold" text-anchor="middle">{letter}</text>'
    for e in edges:
        bands += f'<line x1="{x_at(e):.1f}" y1="{top}" x2="{x_at(e):.1f}" y2="{h - bottom}" stroke="#c8c8d8" stroke-width="1"/>'
    labels = ""
    placed: list[tuple[float, float]] = []
    for label, v, tip in sorted(named, key=lambda item: item[1]):
        x, y = x_at(v), y_at(density(v))
        tx = min(max(x, left + 14), w - right - 14)
        level = sum(1 for px, py in placed if abs(px - tx) < 36 and abs(py - y) < 40)
        placed.append((tx, y))
        ty = max(9.0, y - 8 - 11 * level)
        labels += (
            f'<g><title>{escape(tip)}</title>'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.4" fill="{NAVY}" fill-opacity="0.45"/>'
            f'<text x="{tx:.1f}" y="{ty:.1f}" font-size="9" text-anchor="middle">{escape(label)}</text></g>'
        )
    hits = "".join(f'<circle class="hit" cx="{x_at(v):.1f}" cy="{y_at(density(v)):.1f}" r="3.5" fill="transparent" style="pointer-events:all" data-tip="{escape(tip)}" data-href="{escape(href)}"/>' for v, tip, href in (points or []))
    marker = ""
    if focal is not None:
        fx, fy = x_at(focal), y_at(density(focal))
        anchor = "end" if fx > w * 0.6 else "start"
        dx = -8 if anchor == "end" else 8
        marker = (
            f'<line x1="{fx:.1f}" y1="{fy:.1f}" x2="{fx:.1f}" y2="{h - bottom}" stroke="{NAVY}" stroke-width="1" stroke-dasharray="2 2"/>'
            f'<circle cx="{fx:.1f}" cy="{fy:.1f}" r="4" fill="{NAVY}"/>'
            f'<text x="{fx + dx:.1f}" y="{max(12.0, fy - 6):.1f}" font-size="11" font-weight="bold" text-anchor="{anchor}">{escape(focal_label)}</text>'
        )
    return (
        f'<svg class="chart" viewBox="0 0 {w} {h}" aria-label="Density of peer composite scores with letter bands, this company and the largest peers marked">'
        f'<path d="{area}" fill="{NAVY}" fill-opacity="0.05"/>{bands}'
        f'<path d="{line}" fill="none" stroke="{NAVY}" stroke-width="1.3"/>'
        f'<line x1="{left}" y1="{h - bottom}" x2="{w - right}" y2="{h - bottom}" stroke="{NAVY}" stroke-width="1"/>'
        f"{labels}{hits}{marker}"
        f'<text x="{left}" y="{h - 8}" font-size="10">stronger</text>'
        f'<text x="{w - right}" y="{h - 8}" font-size="10" text-anchor="end">weaker</text>'
        "</svg>"
    )


def load_peers(data: Path, peer_group: str) -> list[dict[str, Any]]:
    path = data / "peer_groups" / f"{peer_group}.json"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as handle:
        rows = json.load(handle).get("tickers", [])
    return [row for row in rows if row.get("grade")]


def peer_factor_medians(data: Path, peers: list[dict[str, Any]]) -> tuple[dict[str, float], dict[str, float]]:
    """Median raw value per input across the peer artifacts, and market equity per peer ticker."""
    values: dict[str, list[float]] = {}
    market_equity: dict[str, float] = {}
    for row in peers:
        try:
            with open(data / row["artifact"], encoding="utf-8") as handle:
                report = json.load(handle)
        except (OSError, ValueError):
            continue
        for factor in report.get("factors", []):
            if isinstance(factor.get("value"), (int, float)):
                values.setdefault(factor["name"], []).append(float(factor["value"]))
        equity = ((report.get("implied_expectations") or {}).get("live_inputs") or {}).get("market_equity")
        if isinstance(equity, (int, float)):
            market_equity[row["ticker"]] = float(equity)
    return {name: statistics.median(v) for name, v in values.items() if v}, market_equity


def render(
    report: dict[str, Any],
    data: Path,
    prices: list[list[Any]] | None = None,
    index: dict[str, Any] | None = None,
    description: dict[str, Any] | None = None,
    page_link: str | None = None,
    rescans: list[tuple[str, str | None, float | None]] | None = None,
    pdf_link: bool = False,
    downloads: bool = False,
) -> str:
    company = report["company"]
    grade = report["grade"]
    ticker = company["ticker"]
    name = _company_name(company["name"])
    cites = Citations()
    peers = load_peers(data, grade.get("peer_group") or company.get("sector") or "")
    medians, peer_mcap = peer_factor_medians(data, peers) if peers else ({}, {})
    factors = {f["name"]: f for f in report["factors"]}
    percentiles = grade.get("factor_percentiles") or {}
    sd = grade.get("sampling_standard_deviation")
    condition = report.get("condition") or {}
    stratum = condition.get("stratum") or {}
    reliability = ((condition.get("reliability") or {}).get("stratum")) or {}
    snapshot = report.get("market_snapshot") or {}
    live = report.get("live_implied_expectations") or {}
    flags = {f["name"]: f for f in report.get("flags", [])}
    active = [f for f in report.get("flags", []) if f.get("active")]
    diagnostics = report.get("diagnostics") or {}
    history = report.get("grade_history") or []
    sector_label = (company.get("sector") or "").replace("_", " ")
    graded = grade.get("status") == "resolved" and grade.get("grade")
    as_of = report["as_of"]

    # ---- masthead and nav
    out: list[str] = [CHARSET, f"<title>Assay {escape(ticker)}</title>", f"<style>{CSS}</style>", WARN_SYMBOL, f'<div class="page" id="top" data-base="{escape(page_link or "./")}">']
    out.append(
        '<div class="center"><h1>Assay</h1>'
        "<p><b>Financial-condition grades for SEC filers</b></p>"
        f"<p><b>Company Report</b> &middot; as of {escape(long_date(as_of))}</p></div><hr>"
    )
    latest_filed = max((f.get("filed") or "" for f in report.get("facts", [])), default=None)
    gap = diagnostics.get("filing_gap") or {}
    base = escape(page_link or "./")
    out.append(nav_bar(page_link or "./"))
    out.append(
        '<table class="nav"><tr><td><ul>'
        f'<li><a href="#grade">{escape(name)} ({escape(ticker)})</a><span class="small">{escape(company.get("exchange") or "")} &middot; <a href="{base}sector-{escape(company.get("sector") or "other")}.html">{escape(sector_label)}</a> &middot; SIC {company.get("sic")}, {escape(company.get("sic_description") or "")}</span></li>'
        f'<li><a href="#inputs">The fourteen inputs</a><span class="small">{grade.get("coverage", {}).get("resolved", 0)} of 14 computable</span></li>'
        f'<li><a href="#sources">Sources</a><span class="small">every receipt, with its SEC filing</span></li>'
        + (f'<li><a href="#downloads">Downloads</a><span class="small">PDF report, artifact JSON, inputs and rescans as CSV</span></li>' if downloads else
           (f'<li><a href="{base}{escape(ticker)}.pdf">PDF report</a><span class="small">the same figures, receipts linked, for printing</span></li>' if pdf_link else ""))
        + f'<li><a href="{base}tickers/{escape(ticker)}.json">Data</a><span class="small">this company\'s artifact with every receipt</span></li>'
        "</ul></td><td><ul>"
        f'<li><a href="#peers">Peer group: {escape(sector_label)}, {grade.get("peer_count") or len(peers)} graded companies</a></li>'
        f'<li><a href="{escape(gap.get("last_filing_url") or "#")}">Latest periodic filing: {escape(gap.get("last_form") or "n/a")} received {escape(long_date(gap.get("last_filing_date")))}</a><span class="small">{gap.get("days")} days before the rescan</span></li>'
        f'<li><a href="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&amp;CIK={company["cik"]:010d}&amp;type=&amp;dateb=&amp;owner=include&amp;count=40">All SEC filings for CIK {company["cik"]:010d}</a></li>'
        "</ul></td></tr></table><hr>"
    )

    # ---- scoreboard and chart
    out.append('<div class="sheet"><div>')
    out.append(f'<table class="score"><tr><th colspan="2">{escape(name)}<span class="sub">{escape(ticker)} &middot; {escape(sector_label)}</span></th></tr>')
    if graded:
        animal, photo = animals.for_letter(grade["grade"])
        portrait = f'<img src="{photo}" alt="{escape(animal)}" style="float:right;width:84px;height:63px;object-fit:cover;border:1px solid #000080;margin:0 0 4px 10px">' if photo else ""
        out.append(
            f'<tr><td class="k">Grade</td><td>{portrait}<span class="big">{escape(grade["grade"])}</span>{ruler(grade["percentile"])}'
            f'<span class="sub">{escape(_fifth(grade["grade"]))} of {grade["peer_count"]} graded peers; percentile {pct_points(grade["percentile"])}, sampling error {pct_points(sd)} points; lower is stronger</span></td></tr>'
        )
        rank = 1 + sum(1 for p in peers if (p.get("percentile") or 0) < grade["percentile"])
        out.append(f'<tr><td class="k">Rank</td><td>{rank} of {len(peers)}</td></tr>')
        for key, label in SLEEVES:
            value = (grade.get("sleeve_scores") or {}).get(key)
            letter = grade_label(value, sd or 0) if value is not None else "n/a"
            out.append(f'<tr><td class="k">{label}</td><td><span class="lt">{escape(letter)}</span>{bar(value)}<span class="sub">percentile {pct_points(value)}</span></td></tr>')
    else:
        out.append(f'<tr><td class="k">Grade</td><td><span class="big">None</span><span class="sub">{escape(grade.get("reason") or "")}: {grade.get("coverage", {}).get("resolved", 0)} of 14 inputs computable; 7 are required</span></td></tr>')
    if reliability:
        out.append(
            f'<tr><td class="k">Reliability</td><td>{reliability["auc"]:.2f} AUC<span class="sub">failure model, filers with {escape(stratum.get("label") or "")}; 95% interval {reliability["ci"][0]:.2f} to {reliability["ci"][1]:.2f}; {reliability["test_events"]} events, 2020 to 2025</span></td></tr>'
            f'<tr><td class="k">Peer failure rate</td><td>{reliability["base_rate_annual"] * 100:.2f}% a year<span class="sub">Item 1.03 bankruptcies, same asset band, 2012 to 2025</span></td></tr>'
        )
    if stratum:
        out.append(f'<tr><td class="k">Size band</td><td>{escape(stratum.get("label") or "")}<span class="sub">total assets {_money(stratum.get("assets") or 0)}{cites.cite([_condition_receipt(stratum.get("receipt"), "assets", stratum.get("assets"))])}</span></td></tr>')
    universe = condition.get("standard_universe") or {}
    if universe.get("inside") is not None:
        universe_receipts = [_condition_receipt(r, concept, universe.get(concept)) for concept, r in (universe.get("receipts") or {}).items()]
        out.append(f'<tr><td class="k">Standard universe</td><td>{"inside" if universe["inside"] else "outside"}<span class="sub">revenue above $1M and assets above $10M{cites.cite(universe_receipts)}</span></td></tr>')
    warn_links = ", ".join(f'<a href="#warnings">{escape(FLAG_LABELS.get(f["name"], f["name"]).lower())}</a>' for f in active if f["name"] != "fortress")
    fortress = "fortress" in {f["name"] for f in active}
    warn_cell = (f'<svg class="ic"><use href="#warn"/></svg>{warn_links}' if warn_links else "none active") + (" &middot; fortress balance sheet" if fortress else "")
    unresolved = [f for f in report.get("flags", []) if f.get("status") == "unresolved"]
    if unresolved:
        warn_cell += f'<span class="sub">not computable: {escape(", ".join(FLAG_LABELS.get(f["name"], f["name"]).lower() for f in unresolved))}</span>'
    out.append(f'<tr><td class="k">Warnings</td><td>{warn_cell}</td></tr>')
    out.append(f'<tr><td class="k">Inputs computable</td><td>{grade.get("coverage", {}).get("resolved", 0)} of 14</td></tr>')
    out.append(f'<tr><td class="k">Graded from</td><td>filings received through {escape(long_date(latest_filed))}<span class="sub">{(condition.get("data_age") or {}).get("days")} days before the rescan; every figure keyed to the SEC receipt date</span></td></tr>')
    price = snapshot.get("price")
    if price is not None:
        out.append(f'<tr><td class="k" data-quote-label="1">Last close</td><td data-quote="{escape(ticker)}"><span class="quote-price">${price:,.2f}</span><span class="sub quote-sub">{escape(str(snapshot.get("price_timestamp") or "")[:10])}; market equity {_money(live.get("market_equity") or 0)}; {escape(snapshot.get("feed") or "")} feed</span></td></tr>')
    out.append('<tr><td class="k">Price used in grade</td><td>None</td></tr></table>')
    out.append("</div><div>")
    filed_dates = [f.get("filed") for f in report.get("facts", []) if f.get("filed")]
    chart = price_chart(prices or [], history, filed_dates, rescans, ticker, base) if prices else ""
    out.append('<table class="score"><tr><th>Share price, twelve months to the rescan<span class="sub">split-adjusted close; a lollipop at every rescan carries the letter held then; baseline ticks are SEC receipt dates of the inputs; ranges and dragging scroll the full history</span></th></tr>')
    if chart:
        out.append(f'<tr><td class="chartcell">{chart}</td></tr>')
    else:
        out.append('<tr><td class="chartcell"><p class="small">No price series supplied for this render.</p></td></tr>')
    if rescans and len(rescans) >= 3:
        out.append(f'<tr><td class="chartcell">{rescan_chart(rescans)}<p class="caption" style="margin:2px 0 0">Percentile at every rescan on file, {escape(long_date(rescans[0][0]))} to {escape(long_date(rescans[-1][0]))}; stronger toward the top. Hover for the date.</p>'
                   '<p class="caption" style="margin:2px 0 0">Open this report as it stood on '
                   f'<select aria-label="rescan date" onchange="if(this.value)location.href=\'{base}as-of.html#{escape(ticker)}~\'+this.value" style="font-family:Times, serif; font-size:12px"><option value="">a rescan date</option>'
                   + ''.join(f'<option value="{escape(d)}">{escape(long_date(d))}</option>' for d, _, _ in reversed(rescans)) + '</select></p></td></tr>')
    if history:
        rows = "".join(f'<tr><td><a href="{base}as-of.html#{escape(ticker)}~{escape(p["date"])}">{escape(long_date(p["date"]))}</a></td><td class="n"><span class="lt">{escape(p.get("grade") or "none")}</span></td><td class="n">{pct_points(p.get("percentile"))}</td><td>{escape(p.get("stratum") or "")}</td></tr>' for p in history)
        out.append(f'<tr><td class="chartcell"><table class="data" style="margin:0"><tr><th>Letter set</th><th class="n">Letter</th><th class="n">Percentile</th><th>Band</th></tr>{rows}</table>'
                   f'<p class="caption" style="margin:2px 0 0">Each date opens the report as it stood on that rescan.</p></td></tr>')
    out.append("</table></div></div>")
    if description and description.get("text"):
        source = description.get("source_url") or ""
        out.append(
            '<div class="business"><p class="small"><b>Business, in the company\'s words.</b> '
            f'Opening of Item 1 of the {escape(description.get("form") or "10-K")} received {escape(long_date(description.get("filed")))}'
            + (f'; <a href="{escape(source)}">the document on sec.gov</a>' if source else "")
            + f'.</p><p>{escape(description["text"])}</p></div>'
        )

    # ---- warnings: only the checks that triggered; a healthy company gets no list
    if active:
        out.append('<h2 id="warnings">Warnings <a class="top" href="#top">[top]</a></h2>')
        out.append('<div class="warnband">')
        for f in active:
            icon = "" if f["name"] == "fortress" else '<svg class="ic"><use href="#warn"/></svg>'
            out.append(f'<div class="item">{icon}<b>{escape(FLAG_LABELS.get(f["name"], f["name"]))}.</b> {escape(f.get("detail") or "")}{cites.cite(f.get("inputs"))}</div>')
        out.append("</div>")
        out.append(f'<p class="small">{len(report.get("flags", []))} checks ran; none of them enters the letter. The checks are listed on the <a href="{base}warnings.html">warnings screen</a>.</p>')

    # ---- inputs
    out.append('<h2 id="inputs">The Fourteen Inputs <a class="top" href="#top">[top]</a></h2>')
    out.append('<p class="small">Lower percentile is stronger. Source numbers open the SEC filing; a range that spans filings opens the sources table.</p>')
    notes: list[str] = []
    out.append('<div class="scroll"><table class="data"><tr><th>Input</th><th>Percentile, 0 to 100</th><th class="n">Value</th><th class="s">Source</th><th class="n">Peer median</th><th class="n">Percentile</th><th class="n">Peers</th><th>Period end</th></tr>')
    for key, label in SLEEVES:
        sleeve_value = (grade.get("sleeve_scores") or {}).get(key)
        head = f"{label}, sleeve percentile {pct_points(sleeve_value)}" if sleeve_value is not None else label
        out.append(f'<tr class="group"><td colspan="8">{escape(head)}</td></tr>')
        for fname, (flabel, kind, note) in FACTORS.items():
            factor = factors.get(fname)
            if factor is None or factor.get("sleeve") != key:
                continue
            if note:
                notes.append(f"<b>{escape(flabel)}.</b> {escape(note[0].upper() + note[1:])}.")
            pc = percentiles.get(fname) or {}
            median = fmt(medians.get(fname), kind) if fname in medians else "n/a"
            if factor.get("value") is not None:
                tip_text = f"{flabel}: percentile {pct_points(pc.get('percentile'))} of {pc.get('peer_count') or 0} peers; lower is stronger"
                out.append(
                    f'<tr><td class="w">{escape(flabel)}</td><td class="b">{bar(pc.get("percentile"), tip=tip_text)}</td>'
                    f'<td class="n">{fmt(factor["value"], kind)}</td><td class="s">{cites.cite(factor.get("inputs"), inline=True)}</td>'
                    f'<td class="n">{median}</td>'
                    f'<td class="n">{pct_points(pc.get("percentile"))}</td><td class="n">{pc.get("peer_count") or ""}</td>'
                    f'<td class="date">{escape(factor.get("period_end") or "")}</td></tr>'
                )
            else:
                out.append(f'<tr><td class="w">{escape(flabel)}</td><td class="b"></td><td class="n">not computable</td><td class="s"></td><td class="n">{median}</td><td class="n"></td><td class="n"></td><td></td></tr>')
                out.append(f'<tr class="reason"><td colspan="8">{escape(factor.get("detail") or factor.get("reason") or "")}</td></tr>')
    out.append("</table></div>")
    if notes:
        out.append('<div class="fn">' + "".join(f"<p>{note}</p>" for note in notes) + "</div>")
    if graded and isinstance(grade.get("composite"), (int, float)):
        out.append(f'<p class="small">Sleeves are equal-weighted; within a sleeve each computable input carries equal weight. Composite {grade.get("composite"):.3f}; sampling error {pct_points(sd)} percentile points on {grade.get("peer_count")} peers.</p>')

    # ---- peers
    out.append(f'<h2 id="peers">Among {len(peers)} Peers <a class="top" href="#top">[top]</a></h2>')
    if peers and graded:
        scored = [p for p in peers if isinstance(p.get("composite"), (int, float))]
        composites = [p["composite"] for p in scored]
        largest = sorted((p for p in scored if peer_mcap.get(p["ticker"]) and p["ticker"] != ticker), key=lambda p: -peer_mcap[p["ticker"]])[:6]
        named = [(p["ticker"], p["composite"], _tip(p, peer_mcap.get(p["ticker"]))) for p in largest]
        hits = [(p["composite"], _tip(p, peer_mcap.get(p["ticker"])), f'{page_link or "./"}{p["ticker"]}.html') for p in scored]
        out.append(density_chart(composites, grade.get("composite"), f'{ticker} {pct_points(grade["percentile"])}', named, hits))
        out.append('<p class="caption">Composite score of every graded peer, smoothed. Vertical rules are the letter band edges at the 20th, 40th, 60th and 80th percentiles. Small dots are the six largest peers by market equity.</p>')
        ordered = sorted(peers, key=lambda p: p.get("percentile") or 0)
        pos = next((i for i, p in enumerate(ordered) if p["ticker"] == ticker), None)
        if pos is not None:
            window = ordered[max(0, pos - 5): pos + 6]
            rows = "".join(
                f'<tr{" class=focal" if p["ticker"] == ticker else ""}><td class="n">{ordered.index(p) + 1}</td><td>{_company_cell(p, peer_mcap.get(p["ticker"]), sd, page_link)}</td><td class="n">{pct_points(p.get("percentile"))}</td><td class="n">{escape(p.get("grade") or "")}</td><td class="w">{escape(", ".join(FLAG_LABELS.get(f, f).lower() for f in (p.get("active_flags") or [])))}</td></tr>'
                for p in window
            )
            out.append(f'<div class="cols"><div class="scroll peers"><table class="data"><tr><th class="n">Rank</th><th>Company</th><th class="n">Percentile</th><th class="n">Letter</th><th>Warnings</th></tr>{rows}</table></div>')
            sens = report.get("grade_sensitivity") or {}
            rows = "".join(f'<tr><td class="w">{escape(POLICY_LABELS.get(k, k.replace("_", " ")))}</td><td class="n"><span class="lt">{escape(v.get("grade") or "none")}</span></td><td class="n">{pct_points(v.get("percentile"))}</td><td class="n">{v.get("peer_count") or ""}</td></tr>' for k, v in sens.items())
            out.append(f'<div class="scroll"><table class="data"><tr><th>Peer policy</th><th class="n">Letter</th><th class="n">Percentile</th><th class="n">Peers</th></tr>{rows}</table>'
                       f'<p class="small">Each letter is one fifth of the {len(peers)} graded peers, about {len(peers) // 5} companies. A boundary letter (A/B, B/C, C/D, D/E) marks a percentile within one sampling error of a band edge.</p></div></div>')

    # ---- price implies
    out.append('<h2 id="price">What the Price Implies <a class="top" href="#top">[top]</a></h2>')
    out.append(f'<p class="small">Market measures at the {escape(str(snapshot.get("price_timestamp") or as_of)[:10])} close. None enters the grade. Percentile among graded peers with the same measure.</p>')
    metrics = {m["name"]: m for m in (report.get("implied_expectations") or {}).get("metrics", [])}
    out.append('<div class="scroll"><table class="data"><tr><th>Measure</th><th class="n">Value</th><th class="n">Peer median</th><th class="n">Percentile</th><th>Reading</th></tr>')
    for mname, (mlabel, kind, high_is) in PRICE_METRICS.items():
        metric = metrics.get(mname)
        if not metric:
            continue
        value = metric.get("value")
        peer_values = [p["implied_expectations"][mname] for p in peers if isinstance((p.get("implied_expectations") or {}).get(mname), (int, float))]
        if value is None:
            out.append(f'<tr><td class="w">{escape(mlabel)}</td><td class="n">not computable</td><td class="n">{fmt(statistics.median(peer_values), kind) if peer_values else "n/a"}</td><td></td><td class="w small">{escape(metric.get("detail") or metric.get("reason") or "")}</td></tr>')
            continue
        share_below = sum(1 for v in peer_values if v < value) / len(peer_values) if peer_values else None
        reading = ""
        if share_below is not None:
            if mname == "momentum_12_1":
                reading = f"hotter than {share_below * 100:.0f} of 100 peers"
            elif mname == "distance_from_52_week_high":
                reading = f"closer to its high than {share_below * 100:.0f} of 100 peers"
            elif mname == "composite_equity_issuance_5y":
                reading = f"more equity-funded than {share_below * 100:.0f} of 100 peers"
            elif mname == "sector_relative_strength":
                reading = f"ahead of {share_below * 100:.0f} of 100 peers"
            else:
                reading = f"pricier than {100 - share_below * 100:.0f} of 100 peers"
        out.append(f'<tr><td class="w">{escape(mlabel)}</td><td class="n">{fmt(value, kind)}</td><td class="n">{fmt(statistics.median(peer_values), kind) if peer_values else "n/a"}</td><td class="n">{share_below * 100:.0f}</td><td class="w">{escape(reading)}</td></tr>')
    out.append("</table></div>")
    inputs = (report.get("implied_expectations") or {}).get("live_inputs") or {}
    if inputs:
        parts = []
        for key, label in (("market_equity", "market equity"), ("ebit", "EBIT"), ("free_cash_flow", "free cash flow"), ("book_equity", "book equity"), ("sales", "sales"), ("debt", "debt"), ("cash", "cash")):
            if isinstance(inputs.get(key), (int, float)):
                parts.append(f"{label} {_money(inputs[key])}")
        shares = inputs.get("shares")
        out.append(f'<p class="small">Inputs: {escape("; ".join(parts))}{cites.cite([shares] if isinstance(shares, dict) else None)}.</p>')

    # ---- diagnostics
    out.append('<h2 id="diagnostics">Diagnostics <a class="top" href="#top">[top]</a></h2>')
    out.append('<p class="small">Diagnostics do not feed the grade.</p><div class="cols even">')
    piot = diagnostics.get("piotroski_f_score") or {}
    comps = piot.get("components") or []
    if comps:
        rows = "".join(
            f'<tr><td class="w">{escape(PIOTROSKI_LABELS.get(c.get("name"), str(c.get("name")).replace("_", " ")))}</td>'
            f'<td class="n">{_piotroski_metric(c)}</td>'
            f'<td>{"&#10003; pass" if c.get("passed") is True else ("&#10007; fail" if c.get("passed") is False else "n/a")}{cites.cite(c.get("inputs"))}</td></tr>'
            for c in comps
        )
        out.append(f'<div class="scroll"><table class="data"><tr><th colspan="3">Piotroski F-score: {escape(str(piot.get("score")))} of {piot.get("maximum") or 9}, period end {escape(piot.get("period_end") or "")}</th></tr>{rows}</table></div>')
    runway = diagnostics.get("cash_runway") or {}
    adv = diagnostics.get("median_dollar_adv") or {}
    late = diagnostics.get("late_filer") or {}
    cash_input = runway.get("cash_input") if isinstance(runway.get("cash_input"), dict) else None
    months = runway.get("months")
    rows = [
        ("Cash and equivalents", _money(cash_input["value"]) if cash_input and isinstance(cash_input.get("value"), (int, float)) else "n/a", cites.cite([cash_input] if cash_input else None), ""),
        ("Operating cash flow, four quarters", _money(runway["trailing_operating_cash_flow"]) if isinstance(runway.get("trailing_operating_cash_flow"), (int, float)) else "n/a", cites.cite(_runway_receipts(runway)), ""),
        ("Months of runway", f"{months:.1f}" if isinstance(months, (int, float)) else "not finite", "", "" if isinstance(months, (int, float)) else (runway.get("detail") or "")),
        ("Late filings, two years", str(len(late.get("filings") or [])), "", "NT 10-K or NT 10-Q"),
        ("Days since last periodic filing", f'{gap.get("days")}' if gap.get("days") is not None else "n/a", "", f'{gap.get("last_form") or ""} received {long_date(gap.get("last_filing_date"))}' if gap.get("days") is not None else ""),
        ("Median daily dollar volume", _money(adv["value"]) if isinstance(adv.get("value"), (int, float)) else "n/a", "", "63 sessions"),
        ("Days since newest grade input", str((condition.get("data_age") or {}).get("days")), "", ""),
    ]
    out.append('<div class="scroll"><table class="data"><tr><th colspan="2">Cash, filings, liquidity</th></tr>' + "".join(
        f'<tr><td>{escape(k)}</td><td class="n">{v}{c}</td></tr>' + (f'<tr class="reason"><td colspan="2">{escape(note)}</td></tr>' if note else "")
        for k, v, c, note in rows) + "</table></div></div>")

    # ---- sources
    if downloads:
        t = escape(ticker)
        out.append('<h2 id="downloads">Downloads <a class="top" href="#top">[top]</a></h2>')
        out.append('<table class="nav"><tr><td><ul>'
                   f'<li><a href="{base}{t}.pdf">PDF report</a><span class="small">this report as a printable file, receipts linked</span></li>'
                   f'<li><a href="{base}tickers/{t}.json">Artifact JSON</a><span class="small">every input, receipt, warning and diagnostic as computed</span></li>'
                   f'<li><a href="{base}downloads/{t}-inputs.csv">Inputs CSV</a><span class="small">the fourteen inputs: value, percentile, peers, period end, receipt</span></li>'
                   f'<li><a href="{base}downloads/{t}-rescans.csv">Rescans CSV</a><span class="small">letter and percentile at every rescan on file</span></li>'
                   f'<li><a href="{base}as-of.html#{t}~{escape(report["as_of"])}">Point in time</a><span class="small">this report as it stood on any rescan date</span></li>'
                   f'<li><a href="{base}data.html">Whole-universe exports</a><span class="small">index CSV, inputs CSV, rescan cube, SQLite database</span></li></ul></td></tr></table>')
    out.append('<h2 id="sources">Sources <a class="top" href="#top">[top]</a></h2>')
    out.append('<p class="small">Every citation on this page. Filing opens the SEC index page for that submission; viewer opens the SEC interactive data for the same filing; series opens the SEC XBRL API record for that tag and company.</p>')
    out.append('<div class="scroll"><table class="data"><tr><th class="n">#</th><th>Concept</th><th>XBRL tag</th><th class="n">Value</th><th>Period end</th><th>Received</th><th>Form</th><th>Links</th></tr>')
    cik = company["cik"]
    for n, r in enumerate(cites.items, 1):
        value = r.get("value")
        shown = (_money(value) if r.get("unit") == "USD" else f"{value:,.0f}" if isinstance(value, (int, float)) else escape(str(value)))
        viewer = f"https://www.sec.gov/cgi-bin/viewer?action=view&cik={cik}&accession_number={escape(str(r.get('accession')))}&xbrl_type=v"
        series = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/{escape(str(r.get('namespace')))}/{escape(str(r.get('tag')))}.json"
        out.append(
            f'<tr id="src-{n}"><td class="n">{n}</td><td>{escape(str(r.get("concept") or "").replace("_", " "))}</td><td class="tag">{escape(str(r.get("namespace")))}:{escape(str(r.get("tag")))}</td>'
            f'<td class="n">{shown}</td><td class="date">{escape(str(r.get("end")))}</td><td class="date">{escape(str(r.get("filed")))}</td><td>{escape(str(r.get("form")))}</td>'
            f'<td><a href="{escape(r["filing_url"])}">filing</a> &middot; <a href="{viewer}">viewer</a> &middot; <a href="{series}">series</a></td></tr>'
        )
    out.append("</table></div>")

    # ---- limitations
    out.append('<h2 id="limitations">Limitations <a class="top" href="#top">[top]</a></h2>')
    items = []
    if graded and sd is not None:
        edge = min(abs(grade["percentile"] - e) for e in (0.2, 0.4, 0.6, 0.8))
        items.append(f"<b>Grade boundary.</b> Percentile {pct_points(grade['percentile'])} is {edge * 100:.1f} points from the nearest band edge; sampling error is {pct_points(sd)} points.")
    items.append("<b>Peer group.</b> Sector assignment from four-digit SIC codes, hand-tuned and published. The grade under three alternative peer policies is tabled above.")
    if index:
        items.append(_size_limitation(index))
    rel = condition.get("reliability") or {}
    if rel:
        items.append(f"<b>Failure model.</b> {escape(rel.get('fit_window') or '')} fit, {escape(rel.get('test_window') or '')} test; status {escape(str(rel.get('status') or '')).replace('_', ' ')}. {escape(rel.get('confirmation') or '')}.")
    items.append("<b>Input validity.</b> Revenue or total assets at or below $100,000 sets the degenerate-inputs flag. An input whose denominator is at or below $100,000 is left not computable rather than ranked.")
    items.append("<b>Returns.</b> No input on this page has a post-2015 confidence interval on returns that excludes zero. The grade ranks reported financial condition and is not a return forecast.")
    out.append("".join(f"<p>{item}</p>" for item in items))

    # ---- footer
    out.append(
        '<hr><div class="footer">'
        f'<p class="small">{escape(report.get("disclaimer") or "Not investment advice. Informational and educational only. Data from SEC EDGAR, may contain errors, is not warranted.")}</p>'
        '<p class="small">If you have any comments about this page, the methodology and every line of code that produced it are in the public repository. However, due to the limited number of personnel, we are unable to provide a direct response.</p>'
        f'{site_map(base)}{credit_footer()}<p class="small">Updated {escape(long_date(as_of))}</p></div></div>'
    )
    out.append(TIP_SCRIPT)
    return "\n".join(out)


def _tip(row: dict[str, Any], mcap: float | None) -> str:
    parts = [f'{row["ticker"]}, {_company_name(row.get("name") or "")}', f'{row.get("grade") or "no letter"}, percentile {pct_points(row.get("percentile"))}']
    if mcap:
        parts.append(f"market equity {_money(mcap)}")
    if row.get("active_flags"):
        parts.append("warnings: " + ", ".join(FLAG_LABELS.get(f, f).lower() for f in row["active_flags"]))
    return "; ".join(parts)


def _company_cell(row: dict[str, Any], mcap: float | None, sd: float | None, page_link: str | None) -> str:
    """Company name with a hover card: letter on the ruler, percentile, sector line, size, price, warnings."""
    name = _company_name(row.get("name") or "")
    label = f'{escape(name)} ({escape(row["ticker"])})'
    anchor = f'<a href="{escape(page_link)}{escape(row["ticker"])}.html">{label}</a>' if page_link else f'<span tabindex="0">{label}</span>'
    letter = row.get("grade") or "none"
    lines = [
        f'<p><b>{escape(name)}</b> ({escape(row["ticker"])})</p>',
        f'<p><span class="big">{escape(letter)}</span>{escape(_fifth(letter)) if row.get("grade") else "no letter"}; percentile {pct_points(row.get("percentile"))}</p>',
        ruler(row["percentile"]) if isinstance(row.get("percentile"), (int, float)) else "",
        f'<p>{escape(row.get("exchange") or "")} &middot; {escape(row.get("sic_description") or "")}</p>',
    ]
    facts = []
    if mcap:
        facts.append(f"market equity {_money(mcap)}")
    if isinstance(row.get("live_price"), (int, float)):
        facts.append(f'last close ${row["live_price"]:,.2f}')
    if facts:
        lines.append(f'<p>{escape("; ".join(facts))}</p>')
    flags = ", ".join(FLAG_LABELS.get(f, f).lower() for f in (row.get("active_flags") or []))
    lines.append(f'<p>{"Warnings: " + escape(flags) if flags else "No active warnings"}; {row.get("coverage", {}).get("resolved", "?")} of 14 inputs</p>')
    return f'<span class="co">{anchor}<span class="card">{"".join(lines)}</span></span>'


SUFFIXES = {
    "INC": "Inc.", "INC.": "Inc.", "CORP": "Corp.", "CORP.": "Corp.", "CO": "Co.", "CO.": "Co.", "LTD": "Ltd.", "LTD.": "Ltd.",
    "PLC": "plc", "LLC": "LLC", "LP": "L.P.", "L.P.": "L.P.", "L.P": "L.P.", "N.V.": "N.V.", "NV": "N.V.", "S.A.": "S.A.", "AG": "AG", "SE": "SE",
}


def _company_name(name: str) -> str:
    """SEC names arrive in capitals or half capitals. Title-case caps words of four letters or more,
    normalize corporate suffixes, keep short caps tokens (3D, CVS, IBM), drop state markers like /DE/."""
    words = []
    for word in name.replace(",", ", ").split():
        if word.startswith("/") or word.endswith("/") or word.upper() in ("/NEW", "NEW/"):
            continue
        bare = word.rstrip(",")
        comma = "," if word.endswith(",") else ""
        if bare.upper() in SUFFIXES:
            words.append(SUFFIXES[bare.upper()] + comma)
        elif bare.isupper() and len(bare.replace(".", "")) >= 4:
            words.append(bare.title() + comma)
        else:
            words.append(word)
    text = " ".join(words).replace(" ,", ",")
    return text.replace("&Amp;", "&").strip(", ")


def _fifth(letter: str) -> str:
    names = {"A": "strongest fifth", "B": "second fifth", "C": "middle fifth", "D": "fourth fifth", "E": "weakest fifth"}
    if "/" in letter:
        return f"on the {letter} boundary"
    return names.get(letter, letter)


def _condition_receipt(receipt: dict[str, Any] | None, concept: str, value: Any) -> dict[str, Any] | None:
    """Condition receipts carry the tag as namespace:tag and no value; give them the factor receipt shape."""
    if not receipt:
        return None
    tag = str(receipt.get("tag") or "")
    namespace, _, bare = tag.rpartition(":")
    return {
        **receipt,
        "namespace": receipt.get("namespace") or namespace,
        "tag": bare or tag,
        "concept": receipt.get("concept") or concept,
        "value": receipt.get("value", value),
        "unit": receipt.get("unit") or "USD",
    }


def _runway_receipts(runway: dict[str, Any]) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for quarter in runway.get("cash_flow_inputs") or []:
        for r in quarter.get("inputs") or []:
            receipts.append(r)
    if isinstance(runway.get("cash_input"), dict):
        receipts.append(runway["cash_input"])
    return receipts


def _size_limitation(index: dict[str, Any]) -> str:
    counts: dict[str, dict[str, int]] = {}
    for row in index.get("tickers", []):
        if row.get("status") != "eligible" or not row.get("grade") or not row.get("stratum"):
            continue
        bucket = counts.setdefault(row["stratum"], {})
        bucket[row["grade"][0]] = bucket.get(row["grade"][0], 0) + 1
    if not counts:
        return "<b>Size.</b> Not measured."
    def share(stratum: str, letter: str) -> str:
        total = sum(counts.get(stratum, {}).values()) or 1
        return f"{counts.get(stratum, {}).get(letter, 0) / total * 100:.0f}%"
    return (
        f"<b>Size.</b> Within a sector the letter tracks size. Among graded filers in the smallest asset band, {share('Q1', 'E')} grade E and {share('Q1', 'A')} grade A; "
        f"in the largest, {share('Q5', 'E')} grade E and {share('Q5', 'A')} grade A. Universe as of {escape(long_date(index.get('as_of')))}."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="render a static company page from an assay output tree")
    parser.add_argument("--data", default="data")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--prices", type=Path, help="JSON map of ticker to [day, close] pairs")
    parser.add_argument("--descriptions", type=Path, help="JSON map of ticker to a description record from assay.describe")
    parser.add_argument("--page-link", help="base path for links to other company pages, such as ./")
    parser.add_argument("--history", type=Path, help="grade change log to draw from instead of the artifact's own entry")
    args = parser.parse_args(argv)
    data = Path(args.data)
    report = json.loads((data / "tickers" / f"{args.ticker.upper()}.json").read_text(encoding="utf-8"))
    if args.history:
        from .history import ticker_history
        report["grade_history"] = ticker_history(json.loads(args.history.read_text(encoding="utf-8")), args.ticker.upper())
    index = json.loads((data / "index.json").read_text(encoding="utf-8")) if (data / "index.json").exists() else None
    prices = None
    if args.prices:
        prices = json.loads(args.prices.read_text(encoding="utf-8")).get(args.ticker.upper())
    description = None
    if args.descriptions:
        description = json.loads(args.descriptions.read_text(encoding="utf-8")).get(args.ticker.upper())
    html = render(report, data, prices, index, description, args.page_link)
    if args.out:
        args.out.write_text(html, encoding="utf-8")
        print(args.out)
    else:
        print(html)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
