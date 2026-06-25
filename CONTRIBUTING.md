## Contributing

Thanks for contributing to **VF Finder**.

Full project documentation: [README.md](README.md).

### Development setup

- Python **3.10+**
- Install deps:

```bash
python -m pip install -r requirements.txt
```

### Tests

```bash
python -m unittest discover -s vf -p "test_*.py"
```

### Notes

- Keep `brouillon/data/` local-only (never commit runs/cookies/logs).
- Keep optional deps optional: GUI and scrapers should fail with actionable messages when missing.

