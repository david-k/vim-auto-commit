import sys
import os
import tempfile
import json
import subprocess


BUCKET_NAME = "notes-1234"

# Utils
#===================================================================================================
def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

def run_command(cmd, stdin=None):
    return subprocess.run(cmd, capture_output=True, check=True, text=True, input=stdin)

def read_file(filename):
    with open(filename) as f:
        return f.read()

def make_bundle_name(instance_name):
    if "." in instance_name:
        raise RuntimeError("Instance name must not contain '.'")

    return f"auto-commit-repo.{instance_name}.bundle"

def make_enc_bundle_name(instance_name):
    if "." in instance_name:
        raise RuntimeError("Instance name must not contain '.'")

    return f"auto-commit-repo.{instance_name}.bundle.enc"

def extract_instance_name(bundle_name: str):
    parts = bundle_name.split(".")
    if parts[0] != "auto-commit-repo":
        return None

    return parts[1]


def pull_from_remote(remote_bundle):
    with tempfile.TemporaryDirectory() as temp_dir:
        os.chdir(repo_dir)

        # Download encrypted bundle from a Backblaze B2 bucket
        enc_bundle_filename = os.path.join(temp_dir, remote_bundle["fileName"])
        run_command(["backblaze-b2", "download_file_by_name", BUCKET_NAME, remote_bundle["fileName"], enc_bundle_filename])

        # Decrypt bundle
        bundle_filename = os.path.join(temp_dir, "decrypted.bundle")
        passphrase = read_file(os.path.join(repo_dir, ".passphrase"))
        run_command(
            [
                "gpg", "-o", bundle_filename,
                "-d",
                "--passphrase-fd", "0", "--batch",
                enc_bundle_filename
            ],
            stdin=passphrase
        )

        # Pull from bundle
        run_command(["git", "pull", bundle_filename])


# Commands
#===================================================================================================
def command_push(repo_dir, instance_name):
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chdir(repo_dir)

            bundle_name = make_bundle_name(instance_name)
            bundle_filename = os.path.join(temp_dir, bundle_name)

            # Export git repo into a bundle
            run_command(["git", "bundle", "create", bundle_filename, "HEAD", "master"])

            # Encrypt bundle
            enc_bundle_name = make_enc_bundle_name(instance_name)
            enc_bundle_filename = os.path.join(temp_dir, enc_bundle_name)
            passphrase = read_file(os.path.join(repo_dir, ".passphrase"))
            run_command(
                [
                    "gpg", "-o", enc_bundle_filename,
                    "--symmetric", "--cipher-algo", "AES256",
                    "--passphrase-fd", "0", "--batch",
                    bundle_filename
                ],
                stdin=passphrase
            )

            #secret-tool lookup server backblaze.com bucket BUCKET_NAME
            #backblaze-b2 authorize_account 00516c321222f5f0000000001

            # Upload encrypted bundle to a Backblaze B2 bucket
            run_command(["backblaze-b2", "upload_file", BUCKET_NAME, enc_bundle_filename, enc_bundle_name])

            run_command(["notify-send", "Notes: Upload successful"])

    except Exception as e:
        run_command(["notify-send", "-u", "critical", "Uploading notes failed:\n\n" + str(e)])


def command_pull(repo_dir, instance_name):
    try:
        counter = 0
        remote_bundles = json.loads(run_command(["backblaze-b2", "ls", BUCKET_NAME, "--json"]).stdout)
        for remote_bundle in remote_bundles:
            remote_instance_name = extract_instance_name(remote_bundle["fileName"])
            if not remote_instance_name or remote_instance_name == instance_name:
                continue

            pull_from_remote(remote_bundle)
            counter += 1

        run_command(["notify-send", f"Notes: Pulled from {counter} repos"])

    except Exception as e:
        run_command(["notify-send", "-u", "critical", "Downloading notes failed:\n\n" + str(e)])


# Main
#===================================================================================================
if len(sys.argv) != 4:
    eprint("Usage: sync-repo <COMMAND> <REPO> <INSTANCE_NAME>")
    sys.exit(1)

command = sys.argv[1]
repo_dir = sys.argv[2]
instance_name = sys.argv[3].replace(".", "_")

if command == "push":
    command_push(repo_dir, instance_name)
elif command == "pull":
    command_pull(repo_dir,  instance_name)
else:
    eprint("Error: invalid command: " + command)
    sys.exit(1)
