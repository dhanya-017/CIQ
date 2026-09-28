"""Step 9 - Stored functions: install every fn_* table macro from ciq/sql/procedures.sql.

Also a command to reinstall them into the existing warehouse after editing procedures.sql (no rebuild needed):
    python -m ciq.build.install_procedures
"""
import duckdb

from ciq import config


def install(con: duckdb.DuckDBPyConnection) -> None:
    con.execute((config.SQL_DIR / "procedures.sql").read_text(encoding="utf-8-sig"))


def run(ctx) -> None:
    """Pipeline step."""
    install(ctx.con)


if __name__ == "__main__":
    con = duckdb.connect(str(config.WAREHOUSE_DB))
    install(con)
    con.close()
    print("stored functions reinstalled")
