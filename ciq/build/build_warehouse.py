"""Build the CIQ warehouse - command-line entry point. The pipeline itself is in ciq/build/pipeline.py.

    python -m ciq.build.build_warehouse                    build from CIQ_INPUT / Data/real_data/master_nsclc.csv
    python -m ciq.build.build_warehouse --input FILE       build from any claims file (.csv/.csv.gz/.tsv/.parquet)
    python -m ciq.build.build_warehouse --validate-only    check the input file only
    python -m ciq.build.build_warehouse --if-changed       rebuild only if the input, rules or code changed
"""
import sys

from ciq.build.pipeline import build, main  # noqa: F401  (build is the programmatic entry point)

if __name__ == "__main__":
    sys.exit(main())
