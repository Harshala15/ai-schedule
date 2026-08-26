from __future__ import annotations

import argparse
from pathlib import Path

from modules import schedule_utils


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a full 96-block penalty schedule from a current-final CSV."
    )
    parser.add_argument("input_file", help="Path to the current-final schedule CSV.")
    parser.add_argument("output_file", help="Path to write the 96-block penalty schedule CSV.")
    parser.add_argument(
        "--total-blocks",
        type=int,
        default=96,
        help="Total number of blocks to emit. Defaults to 96.",
    )
    args = parser.parse_args()

    summary = schedule_utils.write_full_block_schedule_from_llm_schedule(
        Path(args.input_file),
        Path(args.output_file),
        total_blocks=args.total_blocks,
    )

    print(f"Wrote {summary['total_blocks']} blocks to {summary['output_csv']}")
    print(f"Source blocks present: {summary['blocks_present']}")


if __name__ == "__main__":
    main()
