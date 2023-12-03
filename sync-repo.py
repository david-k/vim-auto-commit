import sys
import os
import tempfile
import json
import subprocess
import re
import secrets
import time
from pathlib import Path

from dataclasses import dataclass


NOTESYNC_DIR = ".notesync"
TARGET_BUNDLE_SIZE = 20*1024
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

def read_config(filename):
    config = {}
    with open(filename) as f:
        return json.load(f)

def write_config(filename, config):
    with open(filename, "w") as f:
        json.dump(config, f)


def make_bundle_name(instance_name: str, bundle_no: int, bundle_gen: int, final_gen = False):
    if "." in instance_name:
        raise RuntimeError("Instance name must not contain '.'")

    # Add the current time and some random bytes to ensure that the generated bundle name is unique.
    # This is needed because:
    # - Bundles on the server must never change after they have been uploaded (because each bundle
    #   contains the delta to the previous bundle, so changing a bundle may invalidate all following
    #   bundles)
    # - We upload new bundles without checking if a bundle with that name already exists.
    #
    # By including the instance name, the current time, and some random bytes in the bundle name,
    # the chances of a name collision seem reasonably small.
    timestamp = time.time_ns() // 10**9 # Current timestamp in seconds
    random_str = secrets.token_hex(5)

    gen_flag = "_"
    if final_gen:
        gen_flag = "f"

    bundle_flag = "_"

    return f"{bundle_no:04d}.{bundle_flag}.{bundle_gen:03d}.{gen_flag}.{instance_name}.{timestamp}.{random_str}.bundle"


@dataclass(order=True)
class BundleInfo:
    # Order of the fields is important because it is used for ordering
    number: int
    generation: int
    instance_name: str
    instance_timestamp: int
    is_final_gen: bool
    rand: str

def extract_bundle_info(bundle_name: str):
    parts = bundle_name.split(".")

    return BundleInfo(
        number = int(parts[0]),
        generation = int(parts[2]),
        instance_name = parts[4],
        instance_timestamp = int(parts[5]),
        is_final_gen = parts[3] == "f",
        rand = parts[6]
    )


def get_master_commit_from_bundle(bundle_filename):
    result = run_command(["git", "bundle", "list-heads", bundle_filename, "refs/heads/master"]).stdout.splitlines()
    if len(result) == 0:
        raise RuntimeError("Bundle does not contain master branch ref")

    [commit_id, ref] = result[0].split(" ")
    if ref != "refs/heads/master":
        raise RuntimeError("get_master_commit_from_bundle: expected master ref")

    return commit_id


# Must be called from the git repo directory
def get_required_commit_from_bundle(bundle_filename):
    result = run_command(["git", "bundle", "verify", bundle_filename]).stdout.splitlines()

    for i in range(len(result)):
        line = result[i]
        if line == "The bundle requires this ref:":
            return result[i+1].strip()
        if line == "The bundle records a complete history.":
            return None

    raise RuntimeError("Extracting required commit id from bundle failed")


def is_first_commit_ancestor_of_second(commit_a, commit_b):
    result = subprocess.run(["git", "merge-base", "--is-ancestor", commit_a, commit_b], capture_output=True, text=True)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False

    raise RuntimeError("`git merge-base --is-ancestor` failed: " + result.stdout + "\n" + result.stderr)


# Reading/writing state
#-------------------------------------------------------------------------------
def read_latest_upload_info(repo_dir = "."):
    filename = os.path.join(repo_dir, NOTESYNC_DIR, "latest_upload_info")
    if os.path.isfile(filename):
        return read_config(filename)

    return None

def write_latest_upload_info(config, repo_dir = "."):
    write_config(os.path.join(repo_dir, NOTESYNC_DIR, "latest_upload_info"), config)


# Pulling bundles
#-------------------------------------------------------------------------------
def fetch_from_remote(remote_bundle_name, latest_included_commit_id):
    with tempfile.TemporaryDirectory() as temp_dir:
        # Download encrypted bundle from a Backblaze B2 bucket
        enc_bundle_filename = os.path.join(temp_dir, remote_bundle_name)
        run_command(["backblaze-b2", "download_file_by_name", BUCKET_NAME, remote_bundle_name, enc_bundle_filename])

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
        run_command(["git", "bundle", "verify", bundle_filename])
        run_command(["git", "fetch", bundle_filename])

        # If the bundle contains a more recent commit than what we have uploaded, then update the uploaded commit id.
        # The ancestor check shouldn't actually be needed since we are only ever pulling newer bundles.
        # - Actually, the check is necessary if a bundle was pulled but the script crashed before it
        #   could update the uploaded commit id
        bundle_commit = get_master_commit_from_bundle(bundle_filename)
        if latest_included_commit_id == None or is_first_commit_ancestor_of_second(latest_included_commit_id, bundle_commit):
            write_latest_upload_info({
                "bundle_name": remote_bundle_name,
                "included_commit_id": bundle_commit,
                "required_commit_id": get_required_commit_from_bundle(bundle_filename),
            })

        return bundle_commit


# Pushing bundles
#-------------------------------------------------------------------------------
def create_bundle(bundle_filename, already_uploaded_commit_id = None):
    command = ["git", "bundle", "create", bundle_filename, "HEAD", "master"]

    if already_uploaded_commit_id:
        command += ["^" + already_uploaded_commit_id] # Exclude what we have already uploaded

    # Export git repo into a bundle
    run_command(command)


def encrypt_bundle(bundle_filename, enc_bundle_filename):
    run_command(
        [
            "gpg", "-o", enc_bundle_filename,
            "--symmetric", "--cipher-algo", "AES256",
            "--passphrase-fd", "0", "--batch",
            bundle_filename
        ],
        stdin = read_file(os.path.join(repo_dir, ".passphrase"))
    )


# Fetches the canonical chain of bundles from the server, filtering out any conflicting left-over bundles
def fetch_bundle_chain():
    remote_bundle_files = json.loads(run_command(["backblaze-b2", "ls", BUCKET_NAME, "--json"]).stdout)

    def bundle_sort_key(fileinfo):
        info = extract_bundle_info(fileinfo["fileName"])
        return (info.number, info.generation, fileinfo["uploadTimestamp"], info.instance_name)

    remote_bundle_files.sort(key = bundle_sort_key)

    bundle_chain = []
    processed_bundles = set()
    bundle_number_done = -1
    for remote_bundle_file in remote_bundle_files:
        remote_bundle = extract_bundle_info(remote_bundle_file["fileName"])

        if (remote_bundle.number, remote_bundle.generation) in processed_bundles:
            # If there are multiple bundles with the same bundle number we only process the first one.
            # The second one would be a left-over of a conflicting push operation and should be removed.
            continue

        if remote_bundle.number <= bundle_number_done:
            continue

        if remote_bundle.is_final_gen:
            bundle_number_done = remote_bundle.number

        processed_bundles.add((remote_bundle.number, remote_bundle.generation))
        bundle_chain.append(remote_bundle_file["fileName"])

    return bundle_chain



def check_for_conflict(uploaded_bundle_name_enc):
    remote_bundle_names = list(reversed(fetch_bundle_chain()))

    if not remote_bundle_names:
        # remote_bundle_names should contain at least uploaded_bundle_name_enc
        raise RuntimeError("retrieve_previously_uploaded_bundle_info: List of files is unexpectedly empty")

    if remote_bundle_names[0] != uploaded_bundle_name_enc:
        return True

    if len(remote_bundle_names) >= 2:
        # Check that the bundle we just uploaded does not conflict with the previously uploaded file
        uploaded_bundle = extract_bundle_info(uploaded_bundle_name_enc)
        previous_bundle = extract_bundle_info(remote_bundle_names[1])

        if previous_bundle.number < uploaded_bundle.number:
            # This check may fail if TARGET_BUNDLE_SIZE is decreased later on
            if not previous_bundle.is_final_gen:
                return True
        elif previous_bundle.number == uploaded_bundle.number and previous_bundle.generation < uploaded_bundle.generation:
            if previous_bundle.is_final_gen:
                return True

        # TODO This doesn't really belong in this function
        if not previous_bundle.is_final_gen:
            delete_uploaded_file(remote_bundle_names[1])

    return False


def delete_uploaded_file(enc_bundle_name):
    # For some reason I need to specify --recursive and --withWildcard in order to delete a singel file
    deleted_files = run_command(["backblaze-b2", "rm", "--noProgress", "--recursive", "--withWildcard", BUCKET_NAME, enc_bundle_name]).stdout

    deleted_files = deleted_files.splitlines()
    if len(deleted_files) == 0 or deleted_files[0] != enc_bundle_name:
        raise RuntimeError("Deleting " + enc_bundle_name + " from Backblaze B2 bucket failed")


# Commands
#===================================================================================================
# latest_upload_info:
# - bundle_name
# - included_commit_id: the latest included commit id
# - required_commit_id
def command_push(repo_dir, instance_name):
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chdir(repo_dir)

            current_commit_id = run_command(["git", "rev-parse", "master"]).stdout.splitlines()[0]
            latest_upload_info = read_latest_upload_info()

            latest_included_commit_id = None
            latest_bundle_number = 0
            latest_bundle_generation = 0
            latest_bundle_final = False
            if latest_upload_info:

                latest_included_commit_id = latest_upload_info["included_commit_id"]

                latest_bundle_info = extract_bundle_info(latest_upload_info["bundle_name"])
                latest_bundle_number = latest_bundle_info.number
                latest_bundle_generation = latest_bundle_info.generation
                latest_bundle_final = latest_bundle_info.is_final_gen


            if latest_included_commit_id == current_commit_id:
                return


            # Depending on whether we have reached the target bundle size we either update the
            # latest bundle in-place or create a new bundle

            # Create a new bundle if this is the first time or the previous bundle exceeded the
            # target bundle size
            if not latest_upload_info or latest_bundle_final:
                # Export git repo into a bundle
                bundle_filename = os.path.join(temp_dir, "bundle")
                create_bundle(bundle_filename, latest_included_commit_id)

                bundle_size = os.path.getsize(bundle_filename)
                is_final_gen = bundle_size > TARGET_BUNDLE_SIZE
                bundle_name = make_bundle_name(instance_name, latest_bundle_number + 1, 1, is_final_gen)

                # Encrypt bundle
                enc_bundle_name = bundle_name + ".enc"
                enc_bundle_filename = os.path.join(temp_dir, enc_bundle_name)
                encrypt_bundle(bundle_filename, enc_bundle_filename)

                # Upload encrypted bundle to a Backblaze B2 bucket
                run_command(["backblaze-b2", "upload_file", BUCKET_NAME, enc_bundle_filename, enc_bundle_name])

                if check_for_conflict(enc_bundle_name):
                    delete_uploaded_file(enc_bundle_name)
                    raise RuntimeError("New data available. Please pull and then push again.")

                write_latest_upload_info({
                    "bundle_name": enc_bundle_name,
                    "included_commit_id": current_commit_id,
                    "required_commit_id": latest_included_commit_id,
                })

            # Update latest bundle in-place
            else:
                # Export git repo into a bundle
                bundle_filename = os.path.join(temp_dir, "bundle")
                create_bundle(bundle_filename, latest_upload_info["required_commit_id"])

                bundle_size = os.path.getsize(bundle_filename)
                is_final_gen = bundle_size > TARGET_BUNDLE_SIZE
                bundle_name = make_bundle_name(instance_name, latest_bundle_number, latest_bundle_generation + 1, is_final_gen)

                # Encrypt bundle
                enc_bundle_name = bundle_name + ".enc"
                enc_bundle_filename = os.path.join(temp_dir, enc_bundle_name)
                encrypt_bundle(bundle_filename, enc_bundle_filename)

                # Upload encrypted bundle to a Backblaze B2 bucket
                run_command(["backblaze-b2", "upload_file", BUCKET_NAME, enc_bundle_filename, enc_bundle_name])

                if check_for_conflict(enc_bundle_name):
                    delete_uploaded_file(enc_bundle_name)
                    raise RuntimeError("New data available. Please pull and then push again.")

                write_latest_upload_info({
                    "bundle_name": enc_bundle_name,
                    "included_commit_id": current_commit_id,
                    "required_commit_id": latest_upload_info["required_commit_id"],
                })

    except subprocess.CalledProcessError as e:
        run_command(["notify-send", "-u", "critical", "Uploading notes failed:\n\n" + str(e) + "\n\n" + e.stdout + "\n\n" + e.stderr])
        sys.exit(1)

    except Exception as e:
        run_command(["notify-send", "-u", "critical", "Uploading notes failed:\n\n" + str(e)])
        sys.exit(1)


def command_pull(repo_dir, instance_name):
    try:
        os.chdir(repo_dir)

        counter = 0
        bundle_chain = fetch_bundle_chain()

        latest_upload_info = read_latest_upload_info()
        latest_bundle_info = None
        latest_included_commit_id = None
        if latest_upload_info:
            latest_bundle_info = extract_bundle_info(latest_upload_info["bundle_name"])
            latest_included_commit_id = latest_upload_info["included_commit_id"]

        for remote_bundle_name in bundle_chain:
            remote_bundle = extract_bundle_info(remote_bundle_name)

            if latest_bundle_info:
                # Skip everything we already know about
                if (remote_bundle.number, remote_bundle.generation) <= (latest_bundle_info.number, latest_bundle_info.generation):
                    continue


            latest_included_commit_id = fetch_from_remote(remote_bundle_name, latest_included_commit_id)
            counter += 1

        run_command(["git", "rebase", "FETCH_HEAD"])
        run_command(["notify-send", f"Notes: Pulled {counter} updates"])

    except subprocess.CalledProcessError as e:
        run_command(["notify-send", "-u", "critical", "Downloading notes failed:\n\n" + str(e) + "\n\n" + e.stdout + "\n\n" + e.stderr])
        sys.exit(1)

    except Exception as e:
        run_command(["notify-send", "-u", "critical", "Downloading notes failed:\n\n" + str(e)])
        sys.exit(1)


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
