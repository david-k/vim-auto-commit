import sys
import os
import tempfile
import json
import subprocess
import re
from pathlib import Path


NOTESYNC_DIR = ".notesync"
ONGOING_OPERATION_FILE = "ongoing_operation"

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

def make_bundle_name(instance_name: str, bundle_no: int):
    if "." in instance_name:
        raise RuntimeError("Instance name must not contain '.'")

    return f"{bundle_no:05d}.{instance_name}.bundle"

def extract_bundle_info(bundle_name: str):
    parts = bundle_name.split(".")

    #      bundle no      instance name
    return int(parts[0]), parts[1]

def get_master_commit_from_bundle(bundle_filename):
    result = run_command(["git", "bundle", "list-heads", bundle_filename, "refs/heads/master"]).stdout.splitlines()
    if len(result) == 0:
        raise RuntimeError("Bundle does not contain master branch ref")

    [commit_id, ref] = result[0].split(" ")
    if ref != "refs/heads/master":
        raise RuntimeError("get_master_commit_from_bundle: expected master ref")

    return commit_id

def is_first_commit_ancestor_of_second(commit_a, commit_b):
    result = subprocess.run(["git", "merge-base", "--is-ancestor", commit_a, commit_b], capture_output=True, text=True)
    if result == 0:
        return True
    if result == 1:
        return False

    raise RuntimeError("`git merge-base --is-ancestor` failed: " + result.stderr)


# Reading/writing state
#-------------------------------------------------------------------------------
def read_uploaded_commit_id(repo_dir = "."):
    filename = os.path.join(repo_dir, NOTESYNC_DIR, "latest_uploaded_commit")
    if os.path.isfile(filename):
        return read_file(filename).splitlines()[0]

def write_uploaded_commit_id(commit_id, repo_dir = "."):
    filename = os.path.join(repo_dir, NOTESYNC_DIR, "latest_uploaded_commit")
    with open(filename, "w") as f:
        f.write(commit_id)


def read_downloaded_bundle_no(repo_dir = "."):
    filename = os.path.join(repo_dir, NOTESYNC_DIR, "latest_downloaded_bundle")
    if os.path.isfile(filename):
        return int(read_file(filename).splitlines()[0])

    return 0

def write_downloaded_bundle_no(bundle_no, repo_dir = "."):
    filename = os.path.join(repo_dir, NOTESYNC_DIR, "latest_downloaded_bundle")
    with open(filename, "w") as f:
        f.write(str(bundle_no))


def start_operation(op_type, latest_uploaded_commit, downloaded_bundle_no, repo_dir = "."):
    try:
        with open(os.path.join(repo_dir, NOTESYNC_DIR, ONGOING_OPERATION_FILE), "x") as file:
            file.write(op_type + " " + (latest_uploaded_commit or "-") + " " + str(downloaded_bundle_no))

    except FileExistsError:
        raise RuntimeError("A previous operation crashed or was terminated prematurely")

def finish_operation(repo_dir = "."):
    Path.unlink(os.path.join(repo_dir, NOTESYNC_DIR, ONGOING_OPERATION_FILE))


# Pulling bundles
#-------------------------------------------------------------------------------
def pull_from_remote(remote_bundle):
    with tempfile.TemporaryDirectory() as temp_dir:
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

        run_command(["git", "bundle", "verify", bundle_filename])

        downloaded_bundle_no = read_downloaded_bundle_no()
        uploaded_commit_id = read_uploaded_commit_id()
        start_operation("pull", uploaded_commit_id, downloaded_bundle_no)

        # Pull from bundle
        run_command(["git", "pull", "--rebase", bundle_filename])

        # If the bundle contains a more recent commit than what we have uploaded, then update the uploaded commit id.
        # The ancestor check shouldn't actually be needed since we are only ever pulling newer bundles.
        bundle_commit = get_master_commit_from_bundle(bundle_filename)
        if uploaded_commit_id == None or is_first_commit_ancestor_of_second(uploaded_commit_id, bundle_commit):
            write_uploaded_commit_id(bundle_commit)

        new_bundle_no, _ = extract_bundle_info(remote_bundle["fileName"])
        write_downloaded_bundle_no(new_bundle_no)

        finish_operation()


def b2_fileinfo_to_tuple(fileinfo):
    bundle_no, instance_name = extract_bundle_info(fileinfo["fileName"])
    return (int(bundle_no), fileinfo["uploadTimestamp"], instance_name)


# Pushing bundles
#-------------------------------------------------------------------------------
def create_bundle(bundle_filename, already_uploaded_commit_id = None):
    command = ["git", "bundle", "create", bundle_filename, "HEAD", "master"]

    if already_uploaded_commit_id:
        command += ["^" + already_uploaded_commit_id] # Exclude what we have already uploaded

    # Export git repo into a bundle
    run_command(command)


def check_for_collision(instance_name: str, uploaded_bundle_no: int):
    # For the sorting to work correctly, bundle names must start with the bundle number
    bundle_names = sorted(run_command(["backblaze-b2", "ls", BUCKET_NAME]).stdout.splitlines(), reverse=True)

    for bundle_name in bundle_names:
        bundle_no, remote_instance_name = extract_bundle_info(bundle_name)
        if bundle_no > uploaded_bundle_no:
            continue
        if bundle_no < uploaded_bundle_no:
            break
        if bundle_no == uploaded_bundle_no and remote_instance_name != instance_name:
            return True

    return False


def delete_uploaded_file(enc_bundle_name):
    # For some reason I need to specify --recursive and --withWildcard in order to delete a singel file
    deleted_files = run_command(["backblaze-b2", "rm", "--noProgress", "--recursive", "--withWildcard", BUCKET_NAME, enc_bundle_name]).stdout

    deleted_files = deleted_files.splitlines()
    if len(deleted_files) == 0 or deleted_files[0] != enc_bundle_name:
        raise RuntimeError("Deleting " + enc_bundle_name + " from Backblaze B2 bucket failed")


# Commands
#===================================================================================================
def command_push(repo_dir, instance_name):
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chdir(repo_dir)

            current_commit_id = run_command(["git", "rev-parse", "master"]).stdout.splitlines()[0]
            downloaded_bundle_no = read_downloaded_bundle_no()
            uploaded_commit_id = read_uploaded_commit_id()

            if uploaded_commit_id == current_commit_id:
                return

            # Construct bundle name to upload
            new_bundle_no = downloaded_bundle_no + 1
            bundle_name = make_bundle_name(instance_name, new_bundle_no)
            bundle_filename = os.path.join(temp_dir, bundle_name)

            # Export git repo into a bundle
            create_bundle(bundle_filename, uploaded_commit_id)

            # Encrypt bundle
            enc_bundle_name = bundle_name + ".enc"
            enc_bundle_filename = os.path.join(temp_dir, enc_bundle_name)
            run_command(
                [
                    "gpg", "-o", enc_bundle_filename,
                    "--symmetric", "--cipher-algo", "AES256",
                    "--passphrase-fd", "0", "--batch",
                    bundle_filename
                ],
                stdin = read_file(os.path.join(repo_dir, ".passphrase"))
            )

            start_operation("push", uploaded_commit_id, downloaded_bundle_no)

            # Upload encrypted bundle to a Backblaze B2 bucket
            run_command(["backblaze-b2", "upload_file", BUCKET_NAME, enc_bundle_filename, enc_bundle_name])

            if check_for_collision(instance_name, new_bundle_no):
                delete_uploaded_file(enc_bundle_name)
                finish_operation()
                raise RuntimeError("New data available. Please pull and then push again.")

            # Remember the current commit so we that next time we know not to upload it again (saves bandwidth)
            write_uploaded_commit_id(current_commit_id)
            write_downloaded_bundle_no(new_bundle_no)
            finish_operation()

            run_command(["notify-send", "Notes: Upload successful"])

    except subprocess.CalledProcessError as e:
        run_command(["notify-send", "-u", "critical", "Uploading notes failed:\n\n" + str(e) + "\n\n" + e.stdout + "\n\n" + e.stderr])

    except Exception as e:
        run_command(["notify-send", "-u", "critical", "Uploading notes failed:\n\n" + str(e)])


def command_pull(repo_dir, instance_name):
    try:
        os.chdir(repo_dir)

        counter = 0
        remote_bundle_files = json.loads(run_command(["backblaze-b2", "ls", BUCKET_NAME, "--json"]).stdout)
        remote_bundle_files.sort(key = b2_fileinfo_to_tuple)

        downloaded_bundle_no = read_downloaded_bundle_no()

        processed_bundles = set()
        for remote_bundle in remote_bundle_files:
            bundle_no, remote_instance_name = extract_bundle_info(remote_bundle["fileName"])

            if bundle_no in processed_bundles:
                # If there are multiple bundles with the same bundle number we only process the first one.
                # The second one would be a left-over of a conflicting push operation and should be removed.
                continue

            if bundle_no <= downloaded_bundle_no:
                continue

            pull_from_remote(remote_bundle)
            processed_bundles.add(bundle_no)

            counter += 1

        run_command(["notify-send", f"Notes: Pulled {counter} updates"])

    except subprocess.CalledProcessError as e:
        run_command(["notify-send", "-u", "critical", "Downloading notes failed:\n\n" + str(e) + "\n\n" + e.stdout + "\n\n" + e.stderr])

    except Exception as e:
        run_command(["notify-send", "-u", "critical", "Downloading notes failed:\n\n" + str(e)])


# Main
#===================================================================================================
if len(sys.argv) != 4:
    eprint("Usage: sync-repo <COMMAND> <REPO> <INSTANCE_NAME>")
    sys.exit(1)

command = sys.argv[1]
repo_dir = sys.argv[2]
instance_name = sys.argv[3]
if not re.match(r"^[0-9a-zA-Z_]+$", instance_name):
    eprint("Invalid instance name: " + instance_name)
    sys.exit(1)

Path.mkdir(Path(repo_dir) / NOTESYNC_DIR, exist_ok=True)

if command == "push":
    command_push(repo_dir, instance_name)
elif command == "pull":
    command_pull(repo_dir,  instance_name)
else:
    eprint("Error: invalid command: " + command)
    sys.exit(1)
