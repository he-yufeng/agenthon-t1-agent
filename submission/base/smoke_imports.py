"""Offline dependency and common financial-data format smoke test."""

from importlib.metadata import version
import subprocess
import sys


def check(source: str) -> None:
    """Run each check separately to bound memory under amd64 emulation."""
    subprocess.run([sys.executable, "-c", source], check=True)


def main() -> None:
    check(
        "import numpy as np, talib; "
        "x=np.arange(1.0,8.0); y=talib.SMA(x,3); "
        "assert np.allclose(y[2:],np.arange(2.0,7.0))"
    )
    check("import polars as pl; assert pl.DataFrame({'x':[1,2]}).shape==(2,1)")
    check(
        "from pathlib import Path; from tempfile import TemporaryDirectory; "
        "import numpy as np, pandas as pd, pyarrow as pa, pyarrow.parquet as pq; "
        "d=TemporaryDirectory(); r=Path(d.name); "
        "f=pd.DataFrame({'price':np.arange(1.0,8.0)}); "
        "f.to_csv(r/'f.csv',index=False); f.to_json(r/'f.json',orient='records'); "
        "f.to_excel(r/'f.xlsx',index=False); "
        "pq.write_table(pa.Table.from_pandas(f),r/'f.parquet'); "
        "assert pd.read_csv(r/'f.csv').shape==(7,1); "
        "assert pd.read_json(r/'f.json').shape==(7,1); "
        "assert pd.read_excel(r/'f.xlsx').shape==(7,1); "
        "assert pq.read_table(r/'f.parquet').num_rows==7; d.cleanup()"
    )
    check(
        "from pathlib import Path; from tempfile import TemporaryDirectory; "
        "import matplotlib; matplotlib.use('Agg'); "
        "from matplotlib import pyplot as plt; import seaborn; "
        "d=TemporaryDirectory(); p=Path(d.name)/'plot.png'; "
        "plt.plot([1,2,3]); plt.savefig(p); plt.close(); "
        "assert p.stat().st_size>0; d.cleanup()"
    )
    for module in (
        "arch",
        "backtrader",
        "plotly",
        "requests",
        "scipy",
        "sklearn",
        "statsmodels",
        "tqdm",
        "yfinance",
    ):
        check(f"import {module}")

    distributions = (
        "arch",
        "backtrader",
        "matplotlib",
        "numpy",
        "openpyxl",
        "pandas",
        "plotly",
        "polars",
        "polars-runtime-compat",
        "pyarrow",
        "requests",
        "scikit-learn",
        "scipy",
        "seaborn",
        "statsmodels",
        "ta-lib",
        "tqdm",
        "yfinance",
    )
    print("dependency_smoke=PASS")
    for name in distributions:
        print(f"{name}=={version(name)}")


if __name__ == "__main__":
    main()
