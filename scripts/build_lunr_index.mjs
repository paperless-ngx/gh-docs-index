// scripts/build_lunr_index.mjs
import fs from "node:fs";
import lunr from "lunr";

const docsPath = "out/github-docs.json";
const idxPath  = "out/github-lunr-index.json";

const docs = JSON.parse(fs.readFileSync(docsPath, "utf-8"));

// Build index with default English pipeline (stemming on)
const idx = lunr(function () {
  this.ref("id");
  this.field("title");
  this.field("excerpt");
  this.field("labels");
  for (const d of docs) {
    this.add({
      id: d.id,
      title: d.title || "",
      excerpt: d.excerpt || "",
      labels: (d.labels || []).join(" "),
    });
  }
});

fs.writeFileSync(idxPath, JSON.stringify(idx));
console.log(`Wrote ${idxPath} (${docs.length} docs)`);