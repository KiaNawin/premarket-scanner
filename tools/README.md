# tools/

`validate_output.py` — checks output/<date>/ before uploading to the rebalance page. Kept exactly as
received (criteria are not edited in this project). `python -m portfolio_stage run` calls it automatically:

    python tools/validate_output.py output/<date> holdings.csv
