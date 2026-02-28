#!/usr/bin/env python3
"""
Deploy YouTube Follow to DXP8800 NAS via SSH.

Automates:
  1. SSH connectivity check
  2. Docker availability check
  3. Copying Docker files to the NAS
  4. Building the Docker image
  5. Starting the container with volume mounts
  6. Verifying the deployment

Usage (run from your Mac):
    python3 deploy_youtube_follow.py
    python3 deploy_youtube_follow.py --dry-run

Prerequisites:
    - SSH key-based auth to dxp8800 (nasut@dxp8800)
    - Docker installed on the NAS
    - /volume3/cloud mounted on the NAS
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

# Defaults
DEFAULT_NAS_HOST = "dxp8800"
DEFAULT_NAS_USER = "nasut"
DEFAULT_INSTALL_DIR = "/home/nasut/youtube-follow"
DEFAULT_CONTAINER_NAME = "youtube-follow"
DEFAULT_VOLUME_ROOT = "/volume3/cloud"
DEFAULT_CLAUDE_AUTH = "/volume3/cloud/.claude-auth"
DEFAULT_MEMORY = "2g"
DEFAULT_CPUS = "2"
DEFAULT_PORT = 8081

SCRIPT_DIR = Path(__file__).parent


def ssh_cmd(host, user, command, dry_run=False, timeout=300):
    """Execute a command on the NAS via SSH."""
    full_cmd = ["ssh", f"{user}@{host}", command]
    print(f"  $ ssh {user}@{host} {command}")

    if dry_run:
        print("    [DRY RUN] Skipped")
        return 0, ""

    try:
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split("\n"):
                print(f"    {line}")
        if result.returncode != 0 and result.stderr.strip():
            for line in result.stderr.strip().split("\n"):
                print(f"    [stderr] {line}")
        return result.returncode, result.stdout.strip()
    except subprocess.TimeoutExpired:
        print("    [TIMEOUT] Command timed out")
        return 1, ""
    except Exception as e:
        print(f"    [ERROR] {e}")
        return 1, ""


def scp_file(host, user, local_path, remote_path, dry_run=False):
    """Copy a file to the NAS via scp (-O for legacy protocol, needed by Synology)."""
    cmd = ["scp", "-O", str(local_path), f"{user}@{host}:{remote_path}"]
    print(f"  $ scp {local_path.name} -> {remote_path}")

    if dry_run:
        print("    [DRY RUN] Skipped")
        return 0

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            print(f"    [ERROR] {result.stderr.strip()}")
        return result.returncode
    except Exception as e:
        print(f"    [ERROR] {e}")
        return 1


def step_check_ssh(host, user, dry_run):
    """[1/6] Verify SSH connectivity."""
    print("\n[1/6] Checking SSH connection...")
    code, _ = ssh_cmd(host, user, "echo 'SSH OK'", dry_run)
    return code == 0


def step_check_docker(host, user, dry_run):
    """[2/6] Verify Docker is available."""
    print("\n[2/6] Checking Docker...")
    code, _ = ssh_cmd(host, user, "docker --version", dry_run)
    return code == 0


def step_copy_files(host, user, install_dir, dry_run):
    """[3/6] Copy Docker build files to the NAS."""
    print("\n[3/6] Copying Docker files to NAS...")
    ssh_cmd(host, user, f"mkdir -p {install_dir}", dry_run)

    files = ["Dockerfile", "entrypoint.sh", "crontab"]
    for f in files:
        local = SCRIPT_DIR / f
        if not local.exists():
            print(f"    [ERROR] {local} not found")
            return False
        code = scp_file(host, user, local, f"{install_dir}/{f}", dry_run)
        if code != 0 and not dry_run:
            return False

    return True


def step_build_and_run(host, user, install_dir, container_name,
                       volume_root, claude_auth, memory, cpus, port,
                       dry_run):
    """[4/6] Build image, stop old container, start new one."""
    print("\n[4/6] Building and starting container...")

    # Stop existing
    ssh_cmd(
        host, user,
        f"docker stop {container_name} 2>/dev/null; "
        f"docker rm {container_name} 2>/dev/null; echo 'Cleaned up'",
        dry_run,
    )

    # Ensure Claude auth dir exists
    ssh_cmd(host, user, f"mkdir -p {claude_auth}", dry_run)

    # Build
    code, _ = ssh_cmd(
        host, user,
        f"cd {install_dir} && docker build -t {container_name} .",
        dry_run,
        timeout=600,
    )
    if code != 0 and not dry_run:
        print("  [ERROR] Docker build failed")
        return False

    # Run
    run_cmd = (
        f"docker run -d"
        f" --name {container_name}"
        f" --restart unless-stopped"
        f" --network host"
        f" -v {volume_root}:/data"
        f" -v {claude_auth}:/root/.claude"
        f" --memory={memory}"
        f" --cpus={cpus}"
        f" --log-driver json-file"
        f" --log-opt max-size=5m"
        f" --log-opt max-file=3"
        f" {container_name}"
    )
    code, _ = ssh_cmd(host, user, run_cmd, dry_run)
    return code == 0 or dry_run


def step_verify(host, user, container_name, port, dry_run):
    """[5/6] Verify the container is running."""
    print("\n[5/6] Verifying deployment...")

    if dry_run:
        print("  [DRY RUN] Skipped")
        return True

    print("  Waiting 10 seconds for startup...")
    time.sleep(10)

    code, status = ssh_cmd(
        host, user,
        f"docker inspect --format='{{{{.State.Status}}}}' {container_name}",
    )
    if status != "running":
        print(f"  [WARNING] Container status: {status}")
        print("\n  Recent logs:")
        ssh_cmd(host, user, f"docker logs --tail 20 {container_name}")
        return False

    print("  Container is running")

    # Health check via API
    print(f"\n  Checking API on port {port}...")
    code, _ = ssh_cmd(host, user, f"curl -s http://localhost:{port}/history | head -c 100")

    ssh_cmd(host, user, f"docker logs --tail 5 {container_name}")
    return True


def step_print_instructions(host, user, container_name, port):
    """[6/6] Print post-deployment instructions."""
    print(f"\n[6/6] Post-deployment instructions")
    print()
    print("  One-time Claude CLI authentication:")
    print(f"    ssh {user}@{host} \"docker exec -it {container_name} claude\"")
    print("    Then visit the URL in your browser to authenticate.")
    print()
    print("  Access the dashboard:")
    print(f"    http://192.168.1.16:8080/shared3/cloud/GitHub/YOUTUBE/latest_videos.html")
    print()
    print(f"  API server: http://192.168.1.16:{port}")
    print()
    print("  Check logs:")
    print(f"    ssh {host} \"docker logs {container_name} --tail 20\"")
    print()
    print("  Cron logs:")
    print(f"    ssh {host} \"docker exec {container_name} cat /var/log/youtube-follow.log\"")


def main():
    parser = argparse.ArgumentParser(
        description="Deploy YouTube Follow to DXP8800 NAS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--nas-host", default=DEFAULT_NAS_HOST)
    parser.add_argument("--nas-user", default=DEFAULT_NAS_USER)
    parser.add_argument("--install-dir", default=DEFAULT_INSTALL_DIR)
    parser.add_argument("--volume-root", default=DEFAULT_VOLUME_ROOT)
    parser.add_argument("--claude-auth", default=DEFAULT_CLAUDE_AUTH)
    parser.add_argument("--memory", default=DEFAULT_MEMORY)
    parser.add_argument("--cpus", default=DEFAULT_CPUS)
    parser.add_argument("--port", default=DEFAULT_PORT, type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print("=" * 55)
    print("YouTube Follow Deployment to DXP8800")
    print("=" * 55)
    print(f"  NAS:         {args.nas_user}@{args.nas_host}")
    print(f"  Install:     {args.install_dir}")
    print(f"  Volume root: {args.volume_root}")
    print(f"  Claude auth: {args.claude_auth}")
    print(f"  Memory:      {args.memory}")
    print(f"  CPUs:        {args.cpus}")
    print(f"  Port:        {args.port}")
    if args.dry_run:
        print(f"  Mode:        DRY RUN")

    if not step_check_ssh(args.nas_host, args.nas_user, args.dry_run):
        print("\n[FATAL] Cannot connect via SSH.")
        sys.exit(1)

    if not step_check_docker(args.nas_host, args.nas_user, args.dry_run):
        print("\n[FATAL] Docker not available.")
        sys.exit(1)

    if not step_copy_files(args.nas_host, args.nas_user, args.install_dir, args.dry_run):
        print("\n[FATAL] Failed to copy files.")
        sys.exit(1)

    if not step_build_and_run(
        args.nas_host, args.nas_user, args.install_dir,
        DEFAULT_CONTAINER_NAME, args.volume_root, args.claude_auth,
        args.memory, args.cpus, args.port, args.dry_run,
    ):
        print("\n[FATAL] Failed to build/start container.")
        sys.exit(1)

    if step_verify(args.nas_host, args.nas_user, DEFAULT_CONTAINER_NAME, args.port, args.dry_run):
        print("\n" + "=" * 55)
        print("Deployment complete!")
        step_print_instructions(args.nas_host, args.nas_user, DEFAULT_CONTAINER_NAME, args.port)
        print("=" * 55)
    else:
        print("\n[WARNING] Deployment may have issues. Check logs above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
