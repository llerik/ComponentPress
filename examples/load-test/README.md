# ComponentPress load-test fixture

The project contains three schema-6 components connected to three worksheets in `data/load-test.xlsx`. Each sheet has 25 records, five content columns (three Latin text fields and two image paths), and numeric `Prod`/`Test` counts of 4 and 1. The workbook also embeds 50 row-specific images on each sheet, anchored over the two image columns. The project contains the matching 150 PNG assets.

The text is drawn from the standard Lorem Ipsum passage and the cited Cicero sections on [lipsum.com](https://www.lipsum.com/). `SOURCE.txt` records the source. All image assets are local; network access is not needed to validate or build the project.

Run the repeatable checks and timing series from the repository root:

```powershell
.\.venv\Scripts\python.exe examples/load-test/benchmark.py --repetitions 4
```

This measures the first run and three repeats of each copy mode. `timings.csv` holds per-run measurements; `timings.json` also records medians, input hashes, application source fingerprint, build reports, output hashes and machine details. The script checks all worksheet values and embedded image anchors, performs a fresh full validation of every bound row, then builds and verifies Prod and Test PDFs. PDF previews used for verification are stored in the ignored `.componentpress` folder.

The independent measurements are: three-sheet XLSX read, full project/data/binding validation, input snapshot preparation, PNG rendering, PDF writing, and total build wall time. First iteration means first iteration of the benchmark process; subsequent iterations share the Windows file cache. The report does not claim cold-disk timings.

Regenerate image assets and component/project files with:

```powershell
.\.venv\Scripts\python.exe examples/load-test/create_fixture.py
```

`build-workbook.mjs` is the artifact-tool source used to create the workbook. Workbook previews used for visual inspection are stored in the ignored `.componentpress` folder.
