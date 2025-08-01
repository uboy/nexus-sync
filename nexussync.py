import os
import shutil
import requests
import logging
import time
import json
from datetime import datetime, timezone
import re
import subprocess
import tempfile
import stat

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration file
CONFIG_FILE = './nexus_sync_config.json'

# Directory to store downloaded assets
DOWNLOAD_DIR = './downloaded_assets'

# File to store last sync information
SYNC_STATE_FILE = './nexus_sync_state.json'

# Create the download directory if it doesn't exist
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def create_default_config():
    """Create a default configuration file."""
    default_config = {
        "source": {
            "nexus_url": "https://<source.nexus>",
            "repository": "<reponame>",
            "username": "<login>",
            "password": "<pass>"
        },
        "target": {
            "nexus_url": "<nexus cached proxy>",
            "repository": "<repo>",
            "username": "<username>",
            "password": "<pass>"
        },
        "settings": {
            "batch_size": 10,
            "download_timeout": 60,
            "upload_timeout": 120,
            "request_timeout": 30,
            "batch_delay": 1,
            "max_pages": 1000  # Limit pages for testing
        }
    }

    with open(CONFIG_FILE, 'w') as f:
        json.dump(default_config, f, indent=2)

    logger.info(f"Created default configuration file: {CONFIG_FILE}")
    logger.info("Please update the configuration file with your actual credentials and settings")
    return default_config


def load_config():
    """Load configuration from file."""
    if not os.path.exists(CONFIG_FILE):
        logger.info("Configuration file not found, creating default configuration...")
        return create_default_config()

    try:
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
        logger.info("Configuration loaded successfully")
        return config
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Error loading configuration: {e}")
        logger.info("Creating new default configuration...")
        return create_default_config()


def sanitize_filename(filename):
    """Sanitize filename to be safe for filesystem operations."""
    # Replace problematic characters with safe alternatives
    sanitized = filename.replace('/', '_').replace('\\', '_').replace(':', '_')
    sanitized = sanitized.replace('<', '_').replace('>', '_').replace('"', '_')
    sanitized = sanitized.replace('|', '_').replace('?', '_').replace('*', '_')
    sanitized = sanitized.replace('@', 'at_')

    # Remove any remaining problematic characters
    sanitized = re.sub(r'[^\w\-_.]', '_', sanitized)

    # Replace multiple consecutive underscores with a single one
    sanitized = re.sub(r'_+', '_', sanitized)

    # Remove leading/trailing underscores or invalid characters
    sanitized = sanitized.strip('_.-')

    # Ensure it doesn't start with a dot or dash
    if sanitized.startswith('.') or sanitized.startswith('-'):
        sanitized = 'pkg_' + sanitized

    return sanitized


def load_sync_state():
    """Load the last sync state from file."""
    if os.path.exists(SYNC_STATE_FILE):
        try:
            with open(SYNC_STATE_FILE, 'r') as f:
                state = json.load(f)
                logger.info(f"Loaded sync state: last sync at {state.get('last_sync_date', 'Never')}")
                return state
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Could not load sync state file: {e}")

    return {'last_sync_date': None, 'synced_assets': []}


def save_sync_state(synced_assets):
    """Save the current sync state to file."""
    state = {
        'last_sync_date': datetime.now(timezone.utc).isoformat(),
        'synced_assets': synced_assets,
        'total_synced': len(synced_assets)
    }

    try:
        with open(SYNC_STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2)
        logger.info(f"Saved sync state: {len(synced_assets)} assets synced at {state['last_sync_date']}")
    except IOError as e:
        logger.error(f"Could not save sync state: {e}")


def parse_nexus_date(date_string):
    """Parse Nexus date string to datetime object."""
    try:
        # Nexus typically uses ISO format: 2023-07-31T10:30:45.123+00:00
        # Handle various formats
        for fmt in [
            "%Y-%m-%dT%H:%M:%S.%f%z",  # With microseconds and timezone
            "%Y-%m-%dT%H:%M:%S%z",  # Without microseconds but with timezone
            "%Y-%m-%dT%H:%M:%S.%fZ",  # With microseconds, Z for UTC
            "%Y-%m-%dT%H:%M:%SZ",  # Without microseconds, Z for UTC
        ]:
            try:
                return datetime.strptime(date_string, fmt)
            except ValueError:
                continue

        # If none of the formats work, try parsing without timezone info
        dt = datetime.fromisoformat(date_string.replace('Z', '+00:00'))
        return dt
    except Exception as e:
        logger.warning(f"Could not parse date '{date_string}': {e}")
        return None


def get_repository_type(nexus_url, repository, username, password, timeout=30):
    """Check if the repository is proxy or hosted."""
    url = f"{nexus_url}/service/rest/v1/repositories/{repository}"
    try:
        response = requests.get(url,
                                #auth=(username, password),
                                timeout=timeout)
        response.raise_for_status()
        repo_data = response.json()
        repo_type = repo_data.get('type', '').lower()
        logger.info(f"Repository {repository} is of type: {repo_type}")
        return repo_type
    except requests.exceptions.RequestException as e:
        logger.error(f"Error checking repository type for {repository}: {e}")
        raise


def get_assets(nexus_url, repository, username, password, last_sync_date=None, timeout=30, max_pages=1):
    """Retrieve assets from the source Nexus repository, optionally filtered by date."""
    base_url = f"{nexus_url}/service/rest/v1/assets?repository={repository}"

    # Add date filter if we have a last sync date
    if last_sync_date:
        # Convert to ISO format for Nexus API
        if isinstance(last_sync_date, str):
            last_sync_iso = last_sync_date
        else:
            last_sync_iso = last_sync_date.isoformat()

        logger.info(f"Filtering assets modified since: {last_sync_iso}")

    url = base_url
    assets = []
    filtered_assets = []
    page = 1

    while url and page <= max_pages:
        try:
            logger.info(f"Fetching assets page {page}/{max_pages}...")
            response = requests.get(url, auth=(username, password), timeout=timeout)
            response.raise_for_status()
            data = response.json()

            current_batch = data.get('items', [])
            assets.extend(current_batch)

            # Filter assets by date if we have a last sync date
            if last_sync_date:
                for asset in current_batch:
                    asset_date = parse_nexus_date(asset.get('lastModified', ''))
                    if asset_date:
                        # Convert last_sync_date to datetime if it's a string
                        sync_date = datetime.fromisoformat(last_sync_date.replace('Z', '+00:00')) if isinstance(last_sync_date, str) else last_sync_date
                        if asset_date.tzinfo is None and sync_date.tzinfo is not None:
                            sync_date = sync_date.replace(tzinfo=None)
                        elif asset_date.tzinfo is not None and sync_date.tzinfo is None:
                            asset_date = asset_date.replace(tzinfo=None)

                        if asset_date > sync_date:
                            filtered_assets.append(asset)
                    else:
                        # If we can't parse the date, include the asset to be safe
                        filtered_assets.append(asset)
            else:
                filtered_assets.extend(current_batch)

            logger.info(f"Retrieved {len(current_batch)} assets from page {page}" +
                        (f" ({len([a for a in current_batch if a in filtered_assets])} new/modified)" if last_sync_date else ""))
            continuation_token = data.get('continuationToken')
            if continuation_token and continuation_token != 'None' and page < max_pages:
                url = f"{base_url}&continuationToken={continuation_token}"
                page += 1
            else:
                # No more pages or invalid token
                logger.info(f"Stopping after {page} page(s)")
                break

        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching assets on page {page}: {e}")
            # If we have some assets already, we can continue with partial data
            if filtered_assets:
                logger.warning(f"Continuing with {len(filtered_assets)} assets fetched so far")
                break
            else:
                raise

    logger.info(f"Total assets found: {len(assets)}")
    if last_sync_date:
        logger.info(f"Assets to sync (modified since last sync): {len(filtered_assets)}")

    return filtered_assets if last_sync_date else assets


def download_asset(asset, download_dir, username=None, password=None, timeout=60):
    """Download an asset from the source Nexus to local storage with authentication."""
    asset_url = asset['downloadUrl']
    asset_path = asset['path']

    # Create a safe filename using sanitization
    sanitized_path = sanitize_filename(asset_path)

    # Split the path into components and filter out invalid ones (like '-')
    path_parts = [part for part in sanitized_path.split('_') if part and part != '-']
    filename = path_parts[-1] if path_parts else sanitized_path

    # Create nested directory structure based on valid path parts
    local_dir = os.path.join(download_dir, *path_parts[:-1]) if len(path_parts) > 1 else download_dir
    os.makedirs(local_dir, exist_ok=True)
    local_path = os.path.join(local_dir, filename)

    try:
        # Use authentication if provided
        auth = (username, password) if username and password else None

        with requests.get(asset_url, stream=True, timeout=timeout, auth=auth) as r:
            r.raise_for_status()
            with open(local_path, 'wb') as f:
                shutil.copyfileobj(r.raw, f)

        logger.debug(f"Downloaded: {asset_path} -> {local_path}")
        return local_path

    except requests.exceptions.RequestException as e:
        logger.error(f"Error downloading {asset_path}: {e}")
        logger.debug(f"Download URL: {asset_url}")
        raise
    except OSError as e:
        logger.error(f"File system error for {asset_path}: {e}")
        logger.debug(f"Local path: {local_path}")
        raise


def upload_npm_package(nexus_url, repository, username, password, local_path, npm_path, timeout=120):
    """Upload NPM package to target Nexus using the correct NPM upload endpoint."""
    upload_url = f"{nexus_url}/service/rest/v1/components?repository={repository}"

    try:
        with open(local_path, 'rb') as file:
            # Prepare multipart form data for NPM upload
            files = {
                'npm.asset': (os.path.basename(local_path), file, 'application/octet-stream')
            }
            # Extract package name and version
            if npm_path.startswith('/@'):
                scope = npm_path.split('/')[1]
                name = npm_path.split('/')[2]
                package_name = f"{scope}/{name}"
                filename = npm_path.split('/')[-1]
                package_prefix = f"{name}-"
                package_version = filename[len(package_prefix):].replace('.tgz', '') if filename.startswith(package_prefix) else filename.replace('.tgz', '')
            else:
                package_name = npm_path.split('/')[-2]
                filename = npm_path.split('/')[-1]
                package_version = filename.rsplit('-', 1)[-1].replace('.tgz', '')

            data = {
                'npm.name': package_name,
                'npm.version': package_version
            }
            logger.debug(f"Extracted npm.name: {data['npm.name']}, npm.version: {data['npm.version']}")
            headers = {'Accept': 'application/json'}
            response = requests.post(
                upload_url,
                auth=(username, password),
                files=files,
                data=data,
                headers=headers,
                timeout=timeout
            )
            response.raise_for_status()
            logger.debug(f"Uploaded: {npm_path} to {upload_url}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error uploading {npm_path}: {e}")
        logger.debug(f"Upload URL: {upload_url}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Server response: {e.response.text}")
        raise


def trigger_proxy_cache(nexus_url, repository, npm_path, username, password, timeout=60):
    """Trigger proxy repository to cache the NPM package using npm pack."""
    # Extract package name and version
    if npm_path.startswith('/@'):
        # For scoped packages, include the scope (e.g., @idlizer/arkgen)
        scope = npm_path.split('/')[1]  # e.g., @idlizer
        name = npm_path.split('/')[2]   # e.g., arkgen
        package_name = f"{scope}/{name}"
        # Extract version by removing the package name prefix from the filename
        filename = npm_path.split('/')[-1]
        package_prefix = f"{name}-"
        package_version = filename[len(package_prefix):].replace('.tgz', '') if filename.startswith(package_prefix) else filename.replace('.tgz', '')
    else:
        package_name = npm_path.split('/')[-2]
        filename = npm_path.split('/')[-1]
        package_version = filename.rsplit('-', 1)[-1].replace('.tgz', '')
    package_spec = f"{package_name}@{package_version}"

    # Create a temporary .npmrc file for authentication
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.npmrc') as npmrc_file:
        registry_url = f"{nexus_url}/repository/{repository}/"
        registry_host = registry_url.split('//')[1].rstrip('/')
        npmrc_content = (
            f"registry={registry_url}\n"
            #f"//{registry_host}/:_authToken={base64.b64encode(f'{username}:{password}'.encode()).decode()}\n"
            f"strict-ssl=false\n"
        )
        npmrc_file.write(npmrc_content)
        npmrc_file_path = npmrc_file.name
        logger.debug(f"Created temporary .npmrc at {npmrc_file_path} with content:\n{npmrc_content}")

    # Ensure .npmrc is readable
    try:
        os.chmod(npmrc_file_path, stat.S_IRUSR | stat.S_IWUSR)
        with open(npmrc_file_path, 'r') as f:
            logger.debug(f"Verified .npmrc content: {f.read()}")
    except OSError as e:
        logger.error(f"Failed to set permissions or read {npmrc_file_path}: {e}")
        raise

    # Create a temporary directory for npm pack output
    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            logger.debug(f"Running npm pack for {package_spec} with registry {registry_url}")
            result = subprocess.run(
                ['npm', 'pack', package_spec, '--userconfig', npmrc_file_path, '--pack-destination', temp_dir, '--loglevel', 'verbose', '--registry', registry_url],
                capture_output=True,
                text=True,
                timeout=timeout
            )
            result.check_returncode()
            logger.debug(f"npm pack output: {result.stdout}")
            logger.info(f"Successfully triggered cache for {npm_path} on proxy repository")
            # Clean up the downloaded .tgz file
            for file in os.listdir(temp_dir):
                if file.endswith('.tgz'):
                    os.unlink(os.path.join(temp_dir, file))
        except subprocess.CalledProcessError as e:
            logger.error(f"Error triggering cache for {npm_path}: {e}")
            logger.debug(f"npm pack command: npm pack {package_spec} --userconfig {npmrc_file_path} --pack-destination {temp_dir} --registry {registry_url}")
            logger.debug(f"npm pack output: {e.stderr}")
            if '404' in e.stderr:
                logger.warning(f"Package {npm_path} not found in upstream, cannot cache")
            raise
        except subprocess.TimeoutExpired:
            logger.error(f"Timeout triggering cache for {npm_path} after {timeout} seconds")
            raise
        finally:
            # Clean up the temporary .npmrc file
            try:
                os.unlink(npmrc_file_path)
            except OSError as e:
                logger.warning(f"Could not remove temporary .npmrc: {e}")


def migrate_assets_batch(assets, config):
    """Process assets in batches to avoid overwhelming the servers."""
    settings = config['settings']
    source_config = config['source']
    target_config = config['target']

    batch_size = settings.get('batch_size', 10)
    batch_delay = settings.get('batch_delay', 1)

    total_assets = len(assets)
    successful_uploads = 0
    failed_uploads = 0
    synced_assets = []

    # Check target repository type
    repo_type = get_repository_type(
        target_config['nexus_url'],
        target_config['repository'],
        target_config['username'],
        target_config['password'],
        settings.get('request_timeout', 30)
    )

    for i in range(0, total_assets, batch_size):
        batch = assets[i:i + batch_size]
        logger.info(f"Processing batch {i // batch_size + 1}/{(total_assets + batch_size - 1) // batch_size}")

        for asset in batch:
            # Skip directories (assets without a file extension like .tgz)
            if not asset['path'].endswith('.tgz'):
                logger.info(f"Skipping directory or non-package asset: {asset['path']}")
                continue

            try:
                logger.info(f"Processing {asset['path']}")
                if repo_type == 'proxy':
                    # For proxy repositories, trigger caching with npm pack
                    trigger_proxy_cache(
                        target_config['nexus_url'],
                        target_config['repository'],
                        asset['path'],
                        target_config['username'],
                        target_config['password'],
                        settings.get('download_timeout', 60)
                    )
                    successful_uploads += 1
                    synced_assets.append({
                        'path': asset['path'],
                        'lastModified': asset.get('lastModified'),
                        'syncedAt': datetime.now(timezone.utc).isoformat()
                    })
                    logger.info(f"Successfully triggered cache for: {asset['path']}")
                else:
                    # For hosted repositories, download and upload
                    local_path = download_asset(
                        asset,
                        DOWNLOAD_DIR,
                        source_config['username'],
                        source_config['password'],
                        settings.get('download_timeout', 60)
                    )

                # Upload to target Nexus
                    upload_npm_package(
                        target_config['nexus_url'],
                        target_config['repository'],
                        target_config['username'],
                        target_config['password'],
                        local_path,
                        asset['path'],
                        settings.get('upload_timeout', 120)
                    )

                    successful_uploads += 1
                    synced_assets.append({
                        'path': asset['path'],
                        'lastModified': asset.get('lastModified'),
                        'syncedAt': datetime.now(timezone.utc).isoformat()
                    })
                    logger.info(f"Successfully migrated: {asset['path']}")

                # Clean up individual file after successful upload
                    try:
                        os.remove(local_path)
                    except PermissionError:
                        logger.warning(f"Could not remove {local_path}, will be cleaned up later")

            except Exception as e:
                failed_uploads += 1
                logger.error(f"Failed to {'cache' if repo_type == 'proxy' else 'migrate'} {asset['path']}: {e}")
                continue

        # Small delay between batches to be respectful to the servers
        time.sleep(batch_delay)

    return successful_uploads, failed_uploads, synced_assets


def validate_credentials(config):
    """Validate that both source and target credentials work."""
    source_config = config['source']
    target_config = config['target']
    timeout = config['settings'].get('request_timeout', 30)

    logger.info("Validating source credentials...")
    try:
        response = requests.get(
            f"{source_config['nexus_url']}/service/rest/v1/repositories",
            auth=(source_config['username'], source_config['password']),
            timeout=timeout
        )
        response.raise_for_status()
        logger.info("Source credentials validated successfully")
    except requests.exceptions.RequestException as e:
        logger.error(f"Source credential validation failed: {e}")
        return False

    logger.info("Validating target credentials...")
    try:
        response = requests.get(
            f"{target_config['nexus_url']}/service/rest/v1/repositories",
            #auth=(target_config['username'], target_config['password']),
            timeout=timeout
        )
        response.raise_for_status()
        logger.info("Target credentials validated successfully")
    except requests.exceptions.RequestException as e:
        logger.error(f"Target credential validation failed: {e}")
        return False

    return True


def safe_cleanup(directory):
    """Safely clean up the download directory with Windows-specific handling."""
    if not os.path.exists(directory):
        return

    max_retries = 3
    for attempt in range(max_retries):
        try:
            # First, ensure all file handles are closed by iterating through and closing
            import gc
            gc.collect()  # Force garbage collection

            # Try to remove the directory
            shutil.rmtree(directory)
            logger.info("Successfully cleaned up temporary files")
            return

        except PermissionError as e:
            if attempt < max_retries - 1:
                logger.warning(f"Cleanup attempt {attempt + 1} failed, retrying in 2 seconds...")
                time.sleep(2)
            else:
                logger.warning(f"Could not clean up temporary directory {directory}: {e}")
                logger.warning("Please manually delete the directory when all file handles are released")
        except Exception as e:
            logger.error(f"Unexpected error during cleanup: {e}")
            break


def main():
    """Main migration function."""
    logger.info("Starting NPM package migration...")

    # Load configuration
    config = load_config()

    # Load previous sync state
    sync_state = load_sync_state()
    last_sync_date = sync_state.get('last_sync_date')

    if last_sync_date:
        logger.info(f"Incremental sync mode: checking for assets modified since {last_sync_date}")
    else:
        logger.info("Full sync mode: no previous sync detected")

    # Validate credentials before starting
    if not validate_credentials(config):
        logger.error("Credential validation failed. Please check your configuration.")
        return

    try:
        # Get assets from the source Nexus (filtered by date if available)
        logger.info("Fetching assets from source repository...")
        source_config = config['source']

        assets = get_assets(
            source_config['nexus_url'],
            source_config['repository'],
            source_config['username'],
            source_config['password'],
            last_sync_date,
            config['settings'].get('request_timeout', 30),
            config['settings'].get('max_pages', 1)
        )

        if not assets:
            logger.info("No new or modified assets found since last sync")
            return

        # Process assets in batches
        successful, failed, synced_assets = migrate_assets_batch(assets, config)

        # Save sync state after successful migration
        if synced_assets:
            save_sync_state(synced_assets)

        logger.info(f"Migration completed: {successful} successful, {failed} failed")

    except Exception as e:
        logger.error(f"Migration failed: {e}")
        raise

    finally:
        # Clean up downloaded assets directory
        logger.info("Cleaning up temporary files...")
        safe_cleanup(DOWNLOAD_DIR)


if __name__ == "__main__":
    main()
