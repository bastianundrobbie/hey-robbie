"""Entry point for running the Weather MCP server: python -m mcp_weather

Defaults to the home location passed via --lat/--lon/--place. The location can be
overridden per request via the tools' ``location`` argument.
"""

import argparse

from mcp_weather.server import DEFAULT_LAT, DEFAULT_LON, DEFAULT_NAME, create_server


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Weather MCP Server – aktuelles Wetter + 7-Tage-Vorschau (DWD/Bright Sky)",
    )
    parser.add_argument("--lat", type=float, default=DEFAULT_LAT,
                        help="Home latitude (default: %(default)s)")
    parser.add_argument("--lon", type=float, default=DEFAULT_LON,
                        help="Home longitude (default: %(default)s)")
    parser.add_argument("--place", default=DEFAULT_NAME,
                        help="Home place label (default: %(default)s)")
    args = parser.parse_args()

    server = create_server(home_lat=args.lat, home_lon=args.lon, home_name=args.place)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
