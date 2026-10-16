import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const here = path.dirname(fileURLToPath(import.meta.url));
const sheets = ["ComponentOne", "ComponentTwo", "ComponentThree"];
const text = [
  "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.",
  "Sed ut perspiciatis unde omnis iste natus error sit voluptatem accusantium doloremque laudantium, totam rem aperiam.",
  "At vero eos et accusamus et iusto odio dignissimos ducimus qui blanditiis praesentium voluptatum deleniti atque corrupti.",
  "Ut enim ad minima veniam, quis nostrum exercitationem ullam corporis suscipit laboriosam, nisi ut aliquid ex ea commodi consequatur.",
  "Nam libero tempore, cum soluta nobis est eligendi optio cumque nihil impedit quo minus id quod maxime placeat facere possimus.",
];
const headers = ["Title", "Subtitle", "Body", "Image 1", "Image 2", "Prod", "Test"];
const workbook = Workbook.create();

for (let sheetIndex = 0; sheetIndex < sheets.length; sheetIndex++) {
  const name = sheets[sheetIndex];
  const sheet = workbook.worksheets.add(name);
  sheet.showGridLines = false;
  sheet.getRange("A1:G1").values = [headers];
  sheet.getRange("A1:G1").format = {
    fill: "#17365D",
    font: { name: "Arial", size: 10, bold: true, color: "#FFFFFF" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
  };
  sheet.getRange("A2:G26").values = Array.from({ length: 25 }, (_, row) => [
    text[row % text.length],
    text[(row + 1) % text.length],
    text[(row + 2) % text.length],
    `assets/images/set-${sheetIndex + 1}/field-1/row-${String(row + 1).padStart(2, "0")}.png`,
    `assets/images/set-${sheetIndex + 1}/field-2/row-${String(row + 1).padStart(2, "0")}.png`,
    4,
    1,
  ]);
  sheet.getRange("A2:C26").format = {
    font: { name: "Arial", size: 9, color: "#243248" },
    wrapText: true,
    verticalAlignment: "center",
  };
  sheet.getRange("D2:E26").format = {
    font: { name: "Arial", size: 8, color: "#555555" },
    wrapText: true,
    verticalAlignment: "center",
  };
  sheet.getRange("F2:G26").format = {
    font: { name: "Arial", size: 10, bold: true, color: "#17365D" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    numberFormat: "0",
  };
  sheet.getRange("A1:G26").format.borders = {
    insideHorizontal: { style: "hair", color: "#D9E2F3" },
    bottom: { style: "thin", color: "#B4C6E7" },
  };
  sheet.getRange("A:A").format.columnWidth = 28;
  sheet.getRange("B:B").format.columnWidth = 28;
  sheet.getRange("C:C").format.columnWidth = 40;
  sheet.getRange("D:D").format.columnWidth = 34;
  sheet.getRange("E:E").format.columnWidth = 34;
  sheet.getRange("F:G").format.columnWidth = 9;
  sheet.getRange("A1:G1").format.rowHeight = 30;
  sheet.getRange("A2:G26").format.rowHeight = 62;
  sheet.freezePanes.freezeRows(1);

  for (let row = 0; row < 25; row++) {
    for (let column = 0; column < 2; column++) {
      const imagePath = path.join(here, "assets", "images", `set-${sheetIndex + 1}`, `field-${column + 1}`, `row-${String(row + 1).padStart(2, "0")}.png`);
      const png = await fs.readFile(imagePath);
      sheet.images.add({
        dataUrl: `data:image/png;base64,${png.toString("base64")}`,
        anchor: { from: { row: row + 1, col: column + 3 }, extent: { widthPx: 120, heightPx: 76 } },
      });
    }
  }
}

const book = await SpreadsheetFile.exportXlsx(workbook);
await book.save(path.join(here, "data", "load-test.xlsx"));
for (const sheetName of sheets) {
  const preview = await workbook.render({ sheetName, range: "A1:G26", scale: 0.75, format: "png" });
  const previewDir = path.join(here, ".componentpress", "workbook-previews");
  await fs.mkdir(previewDir, { recursive: true });
  await fs.writeFile(path.join(previewDir, `preview-${sheetName}.png`), new Uint8Array(await preview.arrayBuffer()));
}
const summary = await workbook.inspect({ kind: "sheet,region", sheetId: sheets[0], range: "A1:G4", maxChars: 1500 });
console.log(summary.ndjson);
