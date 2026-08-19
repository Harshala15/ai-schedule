"""Fetch the newest file from an FTP server and save it locally for Bhupalpally.

Defaults are chosen for a Bhupalpally deployment, but all settings can be
overridden through environment variables or CLI flags.
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from ftplib import FTP, error_perm
from pathlib import Path


DEFAULT_HOST = "ftp.enercast.de"
DEFAULT_PORT = 21
DEFAULT_USERNAME = "adani_mundra_solar"
DEFAULT_REMOTE_DIR = "/incoming/powerdata_realtime/"
DEFAULT_LOCAL_DIR = Path("bhupalpally_fetcher") / "downloads"
DEFAULT_FILENAME_PREFIX = "bhupalpally_"
ENV_FILE_PATH = Path(__file__).resolve().with_name(".env")
ROOT_ENV_FILE_PATH = Path(__file__).resolve().parents[1] / ".env"
DATE_RE = re.compile(r"(\d{8})")


@dataclass(frozen=True)
class FTPConfig:
    host: str
    port: int
    username: str
    password: str
    remote_dir: str
    filename_prefix: str
    local_dir: Path


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _read_env_file() -> dict:
    values = {}
    for env_path in (ENV_FILE_PATH, ROOT_ENV_FILE_PATH):
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, separator, value = line.partition("=")
                if not separator:
                    continue
                values[key.strip()] = value.strip().strip('"').strip("'")
        except FileNotFoundError:
            continue
    return values


def build_config(args: argparse.Namespace) -> FTPConfig:
    env_file = _read_env_file()

    def pick(name: str, cli_value, env_name: str, default: str = "") -> str:
        if cli_value not in (None, ""):
            return str(cli_value)
        return _env(env_name, env_file.get(name, default))

    return FTPConfig(
        host=pick("SFTP_HOST", args.host, "SFTP_HOST", DEFAULT_HOST),
        port=int(pick("SFTP_PORT", args.port, "SFTP_PORT", str(DEFAULT_PORT))),
        username=pick("SFTP_USERNAME", args.username, "SFTP_USERNAME", DEFAULT_USERNAME),
        password=pick("SFTP_PASSWORD", args.password, "SFTP_PASSWORD", ""),
        remote_dir=pick("SFTP_REMOTE_DIR", args.remote_dir, "SFTP_REMOTE_DIR", DEFAULT_REMOTE_DIR),
        filename_prefix=pick("SFTP_FILENAME_PREFIX", args.filename_prefix, "SFTP_FILENAME_PREFIX", DEFAULT_FILENAME_PREFIX),
        local_dir=Path(pick("SFTP_LOCAL_DIR", args.local_dir, "SFTP_LOCAL_DIR", str(DEFAULT_LOCAL_DIR))),
    )


def _pick_latest_file(ftp: FTP, remote_dir: str, filename_prefix: str) -> tuple[str, str]:
    try:
        names = ftp.nlst(remote_dir)
    except error_perm as exc:
        raise FileNotFoundError(f"No downloadable files found in remote directory: {remote_dir}") from exc

    entries: list[tuple[str, str]] = []
    for remote_path in names:
        filename = Path(remote_path).name
        if filename in ("", ".", ".."):
            continue
        if filename_prefix and not filename.lower().startswith(filename_prefix.lower()):
            continue
        mtime = ""
        try:
            response = ftp.sendcmd(f"MDTM {remote_path}")
            if response.startswith("213 "):
                mtime = response[4:].strip()
        except Exception:
            pass
        entries.append((remote_path, mtime))

    if not entries:
        raise FileNotFoundError(f"No downloadable files found in remote directory: {remote_dir}")

    def _sort_key(pair: tuple[str, str]) -> tuple[int, int, str]:
        remote_path, mtime = pair
        filename = Path(remote_path).name
        match = DATE_RE.search(filename)
        if match:
            return (1, int(match.group(1)), remote_path)
        if mtime and len(mtime) >= 14 and mtime[:14].isdigit():
            return (0, int(mtime[:14]), remote_path)
        return (0, 0, remote_path)

    entries.sort(key=_sort_key)
    return entries[-1]


def download_latest_file(config: FTPConfig, local_dir: Path | None = None) -> tuple[Path, str, str]:
    if not config.password:
        raise SystemExit("FTP password not provided. Set SFTP_PASSWORD or pass --password.")

    target_dir = local_dir or config.local_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    ftp = FTP()
    ftp.connect(config.host, config.port, timeout=60)
    try:
        ftp.login(user=config.username, passwd=config.password)
        remote_path, mtime = _pick_latest_file(ftp, config.remote_dir, config.filename_prefix)
        local_path = target_dir / Path(remote_path).name
        with open(local_path, "wb") as f:
            ftp.retrbinary(f"RETR {remote_path}", f.write)
        return local_path, remote_path, mtime
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()


def fetch_latest_file(config: FTPConfig) -> Path:
    local_path, remote_path, mtime = download_latest_file(config)
    print(
        f"Downloaded latest file from {config.host}:{config.port}{config.remote_dir} "
        f"-> {local_path.resolve()}"
    )
    print(f"Remote file: {remote_path}")
    print(f"Remote file timestamp: {mtime}")
    return local_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch the latest file from an FTP server.")
    parser.add_argument("--host", default=None, help=f"FTP host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=None, help=f"FTP port (default: {DEFAULT_PORT})")
    parser.add_argument("--username", default=None, help=f"FTP username (default: {DEFAULT_USERNAME})")
    parser.add_argument("--password", default=None, help="FTP password (prefer SFTP_PASSWORD env var)")
    parser.add_argument("--remote-dir", default=None, help=f"Remote directory to scan (default: {DEFAULT_REMOTE_DIR})")
    parser.add_argument("--filename-prefix", default=None, help=f"Filename prefix to keep (default: {DEFAULT_FILENAME_PREFIX})")
    parser.add_argument("--local-dir", default=None, help=f"Local download directory (default: {DEFAULT_LOCAL_DIR})")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = build_config(args)
    fetch_latest_file(config)


if __name__ == "__main__":
    main()
