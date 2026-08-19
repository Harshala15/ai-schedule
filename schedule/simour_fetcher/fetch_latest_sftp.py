"""
Fetch the newest file from an SFTP server and save it locally.

Default values are chosen for the SIMOUR / Enercast setup, but all
connection details can also be provided through environment variables or
CLI flags.

Required dependency:
    pip install paramiko

Example:
    python -m simour_fetcher.fetch_latest_sftp ^
        --remote-dir / ^
        --local-dir simour_fetcher/downloads
"""

from __future__ import annotations

import argparse
import os
import stat
from dataclasses import dataclass
from pathlib import Path

import paramiko


DEFAULT_HOST = "transfer.enercast.de"
DEFAULT_PORT = 22
DEFAULT_USERNAME = "vedanjay"
DEFAULT_REMOTE_DIR = "."
DEFAULT_LOCAL_DIR = Path("simour_fetcher") / "downloads"
ENV_FILE_PATH = Path(__file__).resolve().with_name(".env")


@dataclass(frozen=True)
class SFTPConfig:
    host: str
    port: int
    username: str
    password: str
    remote_dir: str
    local_dir: Path


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _read_env_file() -> dict:
    values = {}
    try:
        for line in ENV_FILE_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator:
                continue
            values[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return values


def build_config(args: argparse.Namespace) -> SFTPConfig:
    env_file = _read_env_file()

    def pick(name: str, cli_value, env_name: str, default: str = "") -> str:
        if cli_value not in (None, ""):
            return str(cli_value)
        return _env(env_name, env_file.get(name, default))

    return SFTPConfig(
        host=pick("SFTP_HOST", args.host, "SFTP_HOST", DEFAULT_HOST),
        port=int(pick("SFTP_PORT", args.port, "SFTP_PORT", str(DEFAULT_PORT))),
        username=pick("SFTP_USERNAME", args.username, "SFTP_USERNAME", DEFAULT_USERNAME),
        password=pick("SFTP_PASSWORD", args.password, "SFTP_PASSWORD", ""),
        remote_dir=pick("SFTP_REMOTE_DIR", args.remote_dir, "SFTP_REMOTE_DIR", DEFAULT_REMOTE_DIR),
        local_dir=Path(pick("SFTP_LOCAL_DIR", args.local_dir, "SFTP_LOCAL_DIR", str(DEFAULT_LOCAL_DIR))),
    )


def _pick_latest_file(sftp: paramiko.SFTPClient, remote_dir: str) -> tuple[str, paramiko.SFTPAttributes]:
    entries = []
    for item in sftp.listdir_attr(remote_dir):
        remote_path = f"{remote_dir.rstrip('/')}/{item.filename}" if remote_dir not in ("", ".") else item.filename
        if item.filename in (".", ".."):
            continue
        # Skip directories and symlinks that cannot be downloaded as files.
        if hasattr(item, "st_mode") and not stat.S_ISREG(item.st_mode):
            continue
        entries.append((remote_path, item))

    if not entries:
        raise FileNotFoundError(f"No downloadable files found in remote directory: {remote_dir}")

    entries.sort(key=lambda pair: (pair[1].st_mtime, pair[0]))
    return entries[-1]


def download_latest_file(config: SFTPConfig, local_dir: Path | None = None) -> tuple[Path, str, int]:
    if not config.password:
        raise SystemExit(
            "SFTP password not provided. Set SFTP_PASSWORD or pass --password."
        )

    target_dir = local_dir or config.local_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    transport = paramiko.Transport((config.host, config.port))
    try:
        transport.connect(username=config.username, password=config.password)
        with paramiko.SFTPClient.from_transport(transport) as sftp:
            remote_path, attrs = _pick_latest_file(sftp, config.remote_dir)
            local_path = target_dir / Path(remote_path).name
            sftp.get(remote_path, str(local_path))
            return local_path, remote_path, attrs.st_mtime
    finally:
        transport.close()


def fetch_latest_file(config: SFTPConfig) -> Path:
    local_path, remote_path, mtime = download_latest_file(config)
    print(
        f"Downloaded latest file from {config.host}:{config.port}{config.remote_dir} "
        f"-> {local_path.resolve()}"
    )
    print(f"Remote file: {remote_path}")
    print(f"Remote file timestamp: {mtime}")
    return local_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch the latest file from an SFTP server.")
    parser.add_argument("--host", default=None, help=f"SFTP host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=None, help=f"SFTP port (default: {DEFAULT_PORT})")
    parser.add_argument("--username", default=None, help=f"SFTP username (default: {DEFAULT_USERNAME})")
    parser.add_argument("--password", default=None, help="SFTP password (prefer SFTP_PASSWORD env var)")
    parser.add_argument("--remote-dir", default=None, help=f"Remote directory to scan (default: {DEFAULT_REMOTE_DIR})")
    parser.add_argument("--local-dir", default=None, help=f"Local download directory (default: {DEFAULT_LOCAL_DIR})")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = build_config(args)
    fetch_latest_file(config)


if __name__ == "__main__":
    main()
