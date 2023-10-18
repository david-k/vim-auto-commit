import sys
import os
import tempfile
import subprocess


BUNDLE_NAME=".auto-commit-repo.bundle"
ENC_BUNDLE_NAME=".auto-commit-repo.enc"


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

def run_command(cmd, stdin=None):
    subprocess.run(cmd, capture_output=True, check=True, text=True, input=stdin)

def read_file(filename):
    with open(filename) as f:
        return f.read()

def command_push(repo_dir):
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chdir(repo_dir)

            bundle_filename = os.path.join(temp_dir, BUNDLE_NAME)

            # Export git repo into a bundle
            run_command(["git", "bundle", "create", bundle_filename, "HEAD", "master"])

            # Encrypt bundle
            enc_bundle_filename = os.path.join(temp_dir, ENC_BUNDLE_NAME)
            passphrase = read_file(".passphrase")
            run_command(
                [
                    "gpg", "-o", enc_bundle_filename,
                    "--symmetric", "--cipher-algo", "AES256",
                    "--passphrase-fd", "0", "--batch",
                    bundle_filename
                ],
                stdin=passphrase
            )

            #secret-tool lookup server backblaze.com bucket notes-1234
            #backblaze-b2 authorize_account 00516c321222f5f0000000001

            # Upload encrypted bundle to a Backblaze B2 bucket
            run_command(["backblaze-b2", "upload_file", "notes-1234", enc_bundle_filename, ENC_BUNDLE_NAME])

            run_command(["notify-send", "Notes: Upload successful"])

    except Exception as e:
        run_command(["notify-send", "-u", "critical", "Uploading notes failed:\n\n" + str(e)])


def command_pull(repo_dir):
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chdir(repo_dir)

            # Download encrypted bundle from a Backblaze B2 bucket
            enc_bundle_filename = os.path.join(temp_dir, ENC_BUNDLE_NAME)
            run_command(["backblaze-b2", "download_file_by_name", "notes-1234", ENC_BUNDLE_NAME, enc_bundle_filename])

            # Decrypt bundle
            bundle_filename = os.path.join(temp_dir, BUNDLE_NAME)
            passphrase = read_file(".passphrase")
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

            run_command(["notify-send", "Notes: Download successful"])

    except Exception as e:
        run_command(["notify-send", "-u", "critical", "Downloading notes failed:\n\n" + str(e)])


# Main
#===================================================================================================
if len(sys.argv) != 3:
    eprint("Usage: sync-repo <COMMAND> <REPO>")
    sys.exit(1)

command = sys.argv[1]
repo_dir = sys.argv[2]

if command == "push":
    command_push(repo_dir)
elif command == "pull":
    command_pull(repo_dir)
else:
    eprint("Error: invalid command: " + command)
    sys.exit(1)
