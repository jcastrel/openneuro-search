# Full-Text Search for OpenNeuro

An unofficial search page for every public [OpenNeuro](https://openneuro.org) dataset. It is
not affiliated with OpenNeuro. It searches each dataset's README, title, authors, BIDS file names,
task labels, PET tracer (`TracerName` from `*_pet.json`, plus `trc-`/`acq-` labels),
participants.tsv columns and participants.json descriptions, and phenotype measure files
(column headers and sidecar descriptions), which OpenNeuro's own search does not fully index.
Filters cover BIDS data type, participant age range and number of subjects.
It runs on GitHub Pages and rebuilds itself every Monday.

## How it works

- `build_index.py` reads OpenNeuro's GitHub mirror ([OpenNeuroDatasets](https://github.com/OpenNeuroDatasets)),
  one repository per dataset. It reads `dataset_description.json`, the README,
  `participants.tsv`/`.json`, `phenotype/*.tsv` headers and `*.json` sidecars, one PET sidecar, and the
  file tree (names only, from a tree-only clone, so no imaging data is downloaded). File names
  are collapsed across subjects and runs into patterns such as
  `func/sub-*_task-ug_run-*_bold.nii.gz`.
- Runs are incremental: it checks each dataset's latest commit with `git ls-remote` and
  re-reads only new or changed datasets. A first build takes roughly 10 to 15 minutes; later
  builds take a few minutes.
- It writes `site/datasets.json` (the index) and `site/meta.json` (build time and counts).
  `site/index.html` is the page; it shows the date of the last build at the top.

## Set up (about 5 minutes)

1. Create a new **public** repository on GitHub (for example `openneuro-search`) and upload
   these files, keeping the folders: `build_index.py`, `README.md`, `site/index.html` and
   `.github/workflows/update.yml`.
   (On the repository page, choose *Add file → Upload files* and drag in the unzipped folder.
   If the `.github` folder is hidden on your computer, create
   `.github/workflows/update.yml` with *Add file → Create new file* and paste its contents.)
2. Go to *Settings → Pages* and set **Source** to **GitHub Actions**.
3. Go to the *Actions* tab, open **Rebuild index and deploy** and click **Run workflow**.
   When it finishes, the page is live at `https://<your-username>.github.io/<repo-name>/`.

After that it rebuilds every Monday. To change the schedule, edit the `cron` line in
`.github/workflows/update.yml`. GitHub pauses scheduled workflows in repositories with
no activity for 60 days; if that happens, click **Enable workflow** on the Actions tab.

## Run it on your own computer

Rebuilding the output from the cache without contacting GitHub: add `--offline`.


```bash
python build_index.py --cache cache --out site
cd site && python -m http.server 8000     # then open http://localhost:8000
```

The page has to be served over HTTP (as above). Opening `index.html` directly from
disk won't load the data.

## Limits

- File patterns are capped at 250 per dataset and READMEs at 20,000 characters.
- Ages come from the first age column in participants.tsv (converted to years when the sidecar
  gives months, weeks or days). Values over 120 without units, or any negative values, are left out.
  Sex counts are skipped when values are numeric codes without `Levels` in the sidecar.
- PET tracer names come from one sidecar per dataset; a few sidecars are stored in a way
  the mirror doesn't serve as plain text.
- Derivatives-only repositories (OpenNeuroDerivatives) are not included.
- Check the dataset's OpenNeuro page for its current version before downloading.
