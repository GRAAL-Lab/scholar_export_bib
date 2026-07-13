# scholar_export_bib

## Python Environment Setup

Select the Python version you want to use:

```bash
PYTHON=python3.12
```

Check it:

```bash
$PYTHON --version
```

Create a virtual environment:

```bash
$PYTHON -m venv .venv
```

Activate it:

```bash
source .venv/bin/activate
```

Upgrade pip:

```bash
python -m pip install --upgrade pip
```

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Or, if there is no `requirements.txt`:

```bash
python -m pip install scholarly lxml
```

Verify the install:

```bash
python -c "import sys, scholarly; print(sys.version); print(scholarly.__file__)"
```

Configure the search in `scholar_export.conf`:

```ini
[search]
keywords = """
("Underwater Robotics" OR "Marine Robotics" OR "AUV" OR "ROV" OR "Subsea Robotics")
AND
("Artificial Intelligence" OR "Autonomous Navigation" OR "SLAM" OR "Sensor Fusion" OR "Digital Twin")
AND
("Marine Monitoring" OR "Ocean Observation" OR "Underwater Inspection" OR "Ocean Exploration" OR "Marine Ecosystems")
"""
start_year = 2000
end_year = 2026
output = exports/publications_2000_2026.bib
txt = true
max_results = 50
delay = 1.0
fill_publications = false
patents = false
citations = false
sort_by = relevance
network_timeout = 30
fallback_to_crossref = true
```

Results are written in the `exports/` folder. If the configured BibTeX output
already exists, the script writes to the next available numbered file, for
example `exports/publications_2000_2026_1.bib`.

Run the script:

```bash
python scholar_export.py
```

Or use a different config file:

```bash
python scholar_export.py --config my_search.conf
```

## Windows Note (Avoid Hanging During Scholar/Captcha)

On some Windows setups, Google Scholar captcha handling may trigger Selenium/
Firefox startup and appear to hang. This project sets a global network timeout
to avoid blocking forever.

If you still see timeouts, increase this in your config:

```ini
network_timeout = 60
```

If Google Scholar still fails with errors like "Cannot fetch from Google Scholar",
the script now automatically falls back to Crossref when
`fallback_to_crossref = true`.

Deactivate when done:

```bash
deactivate
```

## If `venv` Fails On Ubuntu/Debian

Install the venv package for your Python version:

```bash
sudo apt update
sudo apt install python3.12-venv
```

Then recreate the environment:

```bash
rm -rf .venv
python3.12 -m venv .venv
```

## Fallback: Using `virtualenv`

If built-in `venv` is unavailable, install `virtualenv`:

```bash
python3 -m pip install --user --upgrade virtualenv
```

Then create the environment with your chosen Python:

```bash
~/.local/bin/virtualenv -p python3.12 .venv
```

Then continue normally:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
python scholar_export.py
```
