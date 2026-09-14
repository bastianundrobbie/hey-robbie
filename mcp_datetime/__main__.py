"""Entry point for running the DateTime MCP server: python -m mcp_datetime"""

import argparse

from mcp_datetime.server import create_server


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DateTime MCP Server – Datum, Uhrzeit und Kalenderinfos",
    )
    parser.add_argument(
        "--timezone",
        default="Europe/Berlin",
        help="IANA timezone (default: %(default)s)",
    )
    args = parser.parse_args()

    server = create_server(args.timezone)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
