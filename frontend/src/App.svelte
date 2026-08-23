<script lang="ts">
  import { onMount } from "svelte";
  import { loadIndex, searchCompanies, type IndexArtifact, type IndexRow } from "./lib/assay";

  let index = $state<IndexArtifact | null>(null);
  let query = $state("");
  let error = $state("");

  onMount(async () => {
    try {
      index = await loadIndex();
    } catch (cause) {
      error = cause instanceof Error ? cause.message : "Could not load Assay data.";
    }
  });

  let matches: IndexRow[] = $derived(index ? searchCompanies(index.tickers, query) : []);
</script>

<svelte:head>
  <title>Assay frontend foundation</title>
</svelte:head>

<main>
  <p class="eyebrow">Assay frontend foundation</p>
  <h1>The data layer is connected.</h1>
  {#if error}
    <p role="alert">{error}</p>
  {:else if !index}
    <p aria-live="polite">Loading the current universe…</p>
  {:else}
    <p>{index.summary.tickers.toLocaleString()} SEC tickers as of {index.as_of}.</p>
    <label>
      Test company search
      <input bind:value={query} autocomplete="off" placeholder="AAPL or Apple" />
    </label>
    {#if query.trim()}
      <ul>
        {#each matches as company (company.ticker)}
          <li><strong>{company.ticker}</strong> {company.name} · {company.grade ?? company.reason ?? company.status}</li>
        {/each}
      </ul>
    {/if}
  {/if}
  <p class="note">This is a plumbing check, not the product design.</p>
</main>

<style>
  main {
    width: min(42rem, calc(100% - 2rem));
    margin: 12vh auto;
  }

  .eyebrow,
  .note {
    color: #68675f;
  }

  h1 {
    max-width: 12ch;
    font-size: clamp(2.5rem, 9vw, 5rem);
    font-weight: 500;
    letter-spacing: -0.055em;
    line-height: 0.95;
  }

  label {
    display: grid;
    gap: 0.5rem;
    margin-top: 2rem;
  }

  input {
    width: 100%;
    padding: 0.9rem 1rem;
    border: 1px solid #b9b6aa;
    border-radius: 0;
    background: #fffef9;
  }

  ul {
    padding-left: 1.2rem;
    line-height: 1.8;
  }

  .note {
    margin-top: 3rem;
    font-size: 0.875rem;
  }
</style>
