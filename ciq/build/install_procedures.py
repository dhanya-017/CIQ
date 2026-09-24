"""Reinstall stored functions into an existing warehouse (after editing procedures.sql)."""
import duckdb

from ciq import config
from ciq.build.build_warehouse import install_procedures

if __name__ == "__main__":
    con = duckdb.connect(str(config.WAREHOUSE_DB))
    install_procedures(con)
    con.close()
    print("stored functions reinstalled")
