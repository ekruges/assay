import { lstat, mkdir, readFile, readlink, symlink, unlink } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const source = resolve(here, "../../data");
const target = resolve(here, "../public/data");

await readFile(resolve(source, "index.json"));
await mkdir(dirname(target), { recursive: true });

try {
  const entry = await lstat(target);
  if (!entry.isSymbolicLink()) {
    throw new Error(`${target} exists and is not a symlink; refusing to replace it`);
  }
  if (resolve(dirname(target), await readlink(target)) === source) process.exit(0);
  await unlink(target);
} catch (error) {
  if (error?.code !== "ENOENT") throw error;
}

await symlink(source, target, "dir");
