# Load-test results

## Workbook and output

- Three worksheets, each with 25 records and 5 content columns: 3 Lorem Ipsum text fields and 2 project image paths. `Prod` is 4 and `Test` is 1 on every row.
- 150 embedded worksheet images and 150 matching project image files.
- Every run validated 75 instances. Each Prod build produced 300 copies on 34 A4 PDF pages; each Test build produced 75 copies on 9 pages.
- All 8 builds succeeded. The validation script opened every PDF and rendered its first and last page. The workbook SHA-256 was checked before and after each build.

## Median times

Times cover four runs per mode. Each row is a separate phase; the phase times are included in total build time and should not be added to it.

| Measurement | Prod | Test |
| --- | ---: | ---: |
| Read all 3 Excel sheets | 0.241 s | 0.230 s |
| Full Excel and binding validation | 0.911 s | 0.765 s |
| Prepare build snapshot | 1.480 s | 1.192 s |
| Render 75 PNGs | 2.735 s | 2.181 s |
| Write print PDF | 7.939 s | 1.572 s |
| Complete build | 12.380 s | 5.146 s |

The first iteration is the first build in its benchmark process; later iterations share the Windows file cache. These results do not represent cold-disk startup. The benchmark starts Qt before timing, so process and application startup are excluded.

## Test environment

Windows 10 (build 19045), Python 3.14.7, ComponentPress 0.16.7, PySide6/Qt 6.11.2, 12 logical CPUs, 31.78 GiB RAM, 2 render workers, 300 DPI.

The application worktree had existing uncommitted changes. Git HEAD: `fd7db4a9ca4a33d69916c83b728ee544fb2c8a55`. SHA-256 of the `src/componentpress` Python source tree used: `785cd8bd54328dbd267762c9e26fe5361b82a90cd9ed314190c64477c3e60f52`.

## Repeat

Run from the repository root:

```powershell
.\.venv\Scripts\python.exe examples/load-test/benchmark.py --repetitions 4
```

Raw observations, medians, source hashes, hardware details and build reports are in `timings.csv` and `timings.json`.
