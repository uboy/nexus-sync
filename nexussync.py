import argparse
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
from urllib.parse import quote, urlparse

# Nexus API reference https://help.sonatype.com/en/api-reference.html

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
        "proxy": {
            "http": "",
            "https": "",
            "no_proxy": ""
        },
        "settings": {
            "batch_size": 10,
            "download_timeout": 60,
            "upload_timeout": 120,
            "request_timeout": 30,
            "batch_delay": 1,
            "max_pages": 1000
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


def get_proxies(config):
    """Get proxy settings from config or environment variables."""
    proxies = {}

    config_proxy = config.get('proxy', {})
    http_proxy = config_proxy.get('http') or os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy')
    https_proxy = config_proxy.get('https') or os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
    no_proxy = config_proxy.get('no_proxy') or os.environ.get('NO_PROXY') or os.environ.get('no_proxy')

    if http_proxy:
        proxies['http'] = http_proxy
    if https_proxy:
        proxies['https'] = https_proxy

    return proxies, no_proxy


def sanitize_filename(filename):
    """Sanitize filename to be safe for filesystem operations."""
    sanitized = filename.replace('/', '_').replace('\\', '_').replace(':', '_')
    sanitized = sanitized.replace('<', '_').replace('>', '_').replace('"', '_')
    sanitized = sanitized.replace('|', '_').replace('?', '_').replace('*', '_')
    sanitized = sanitized.replace('@', 'at_')
    sanitized = re.sub(r'[^\w\-_.]', '_', sanitized)
    sanitized = re.sub(r'_+', '_', sanitized)
    sanitized = sanitized.strip('_.-')

    if sanitized.startswith('.') or sanitized.startswith('-'):
        sanitized = 'pkg_' + sanitized

    return sanitized


def now_utc_iso():
    return datetime.now(timezone.utc).isoformat()


def default_package_state():
    return {
        'etag': None,
        'last_modified': None,
        'known_versions': [],
        'mirrored_versions': [],
        'pending_versions': [],
        'last_checked': None,
        'last_synced': None,
        'metadata_initialized': False,
        'deleted': False,
    }


def sorted_unique(values):
    return sorted(set(values or []))


def merge_unique(existing_values, new_values):
    return sorted(set(existing_values or []) | set(new_values or []))


def format_sample_list(values, limit=5):
    values = list(values or [])
    if not values:
        return 'none'
    preview = values[:limit]
    suffix = '' if len(values) <= limit else f" ... (+{len(values) - limit} more)"
    return ', '.join(preview) + suffix


def extract_package_name_from_path(path):
    """Extract npm package name from a Nexus asset path."""
    clean_path = (path or '').strip('/')
    if not clean_path:
        return None

    parts = clean_path.split('/')
    if parts[0].startswith('@'):
        if len(parts) < 2:
            return None
        return f"{parts[0]}/{parts[1]}"

    return parts[0]


def extract_version_from_path(path):
    """Extract npm package version from a Nexus tarball path."""
    if not path or not path.endswith('.tgz'):
        return None

    filename = os.path.basename(path)
    package_name = extract_package_name_from_path(path)
    if not package_name:
        return None

    package_basename = package_name.split('/')[-1]
    prefix = f"{package_basename}-"
    if filename.startswith(prefix):
        return filename[len(prefix):].replace('.tgz', '')

    return filename.rsplit('-', 1)[-1].replace('.tgz', '')


def extract_package_name_from_asset(asset):
    npm_data = asset.get('npm', {}) if isinstance(asset, dict) else {}
    return npm_data.get('name') or extract_package_name_from_path(asset.get('path', ''))


def extract_version_from_asset(asset):
    npm_data = asset.get('npm', {}) if isinstance(asset, dict) else {}
    return npm_data.get('version') or extract_version_from_path(asset.get('path', ''))


def normalize_sync_state(raw_state):
    """Migrate legacy sync state to the richer package catalog format."""
    raw_state = raw_state or {}
    legacy_synced_assets = raw_state.get('synced_assets') or []
    known_packages = raw_state.get('known_packages') or {}

    normalized_known_packages = {}
    for package_name, package_state in known_packages.items():
        current = default_package_state()
        current.update({
            'etag': package_state.get('etag'),
            'last_modified': package_state.get('last_modified'),
            'known_versions': sorted_unique(package_state.get('known_versions')),
            'mirrored_versions': sorted_unique(package_state.get('mirrored_versions')),
            'pending_versions': sorted_unique(package_state.get('pending_versions')),
            'last_checked': package_state.get('last_checked'),
            'last_synced': package_state.get('last_synced'),
            'metadata_initialized': bool(package_state.get('metadata_initialized', False)),
            'deleted': bool(package_state.get('deleted', False)),
        })
        normalized_known_packages[package_name] = current

    for asset in legacy_synced_assets:
        if not isinstance(asset, dict):
            continue
        path = asset.get('path')
        package_name = extract_package_name_from_path(path)
        version = extract_version_from_path(path)
        if not package_name or not version:
            continue

        package_state = normalized_known_packages.setdefault(package_name, default_package_state())
        package_state['known_versions'] = merge_unique(package_state['known_versions'], [version])
        package_state['mirrored_versions'] = merge_unique(package_state['mirrored_versions'], [version])
        package_state['last_synced'] = asset.get('syncedAt') or package_state.get('last_synced')

    last_sync_date = raw_state.get('last_sync_date')
    return {
        'schema_version': 2,
        'last_sync_date': last_sync_date,
        'last_discovery_date': raw_state.get('last_discovery_date', last_sync_date),
        'known_packages': normalized_known_packages,
        'synced_assets': legacy_synced_assets,
        'total_synced': raw_state.get('total_synced', len(legacy_synced_assets)),
    }


def ensure_package_state(sync_state, package_name):
    package_state = sync_state['known_packages'].setdefault(package_name, default_package_state())
    package_state['known_versions'] = sorted_unique(package_state.get('known_versions'))
    package_state['mirrored_versions'] = sorted_unique(package_state.get('mirrored_versions'))
    package_state['pending_versions'] = sorted_unique(package_state.get('pending_versions'))
    return package_state


def load_sync_state():
    """Load the last sync state from file and normalize it."""
    raw_state = {}
    if os.path.exists(SYNC_STATE_FILE):
        try:
            with open(SYNC_STATE_FILE, 'r') as f:
                raw_state = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Could not load sync state file: {e}")

    state = normalize_sync_state(raw_state)
    logger.info(
        "Loaded sync state: last sync at %s, last discovery at %s, %s known package(s)",
        state.get('last_sync_date', 'Never'),
        state.get('last_discovery_date', 'Never'),
        len(state.get('known_packages', {}))
    )
    return state


def save_sync_state(sync_state):
    """Save the current sync state to file."""
    state_to_save = normalize_sync_state(sync_state)

    try:
        with open(SYNC_STATE_FILE, 'w') as f:
            json.dump(state_to_save, f, indent=2, sort_keys=True)
        logger.info(
            "Saved sync state: %s known package(s), last sync %s, last discovery %s",
            len(state_to_save.get('known_packages', {})),
            state_to_save.get('last_sync_date'),
            state_to_save.get('last_discovery_date')
        )
    except IOError as e:
        logger.error(f"Could not save sync state: {e}")


def parse_nexus_date(date_string):
    """Parse Nexus date string to datetime object."""
    try:
        for fmt in [
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
        ]:
            try:
                return datetime.strptime(date_string, fmt)
            except ValueError:
                continue

        return datetime.fromisoformat(date_string.replace('Z', '+00:00'))
    except Exception as e:
        logger.warning(f"Could not parse date '{date_string}': {e}")
        return None


def get_repository_type(nexus_url, repository, username, password, timeout=30, proxies=None):
    """Check if the repository is proxy or hosted."""
    url = f"{nexus_url}/service/rest/v1/repositories/{repository}"
    try:
        response = requests.get(
            url,
            auth=(username, password) if username or password else None,
            timeout=timeout,
            proxies=proxies
        )
        response.raise_for_status()
        repo_data = response.json()
        repo_type = repo_data.get('type', '').lower()
        logger.info(f"Repository {repository} is of type: {repo_type}")
        return repo_type
    except requests.exceptions.RequestException as e:
        logger.error(f"Error checking repository type for {repository}: {e}")
        raise


def get_assets(nexus_url, repository, username, password, last_sync_date=None, timeout=30, max_pages=1, proxies=None):
    """Retrieve assets from the source Nexus repository, optionally filtered by date."""
    base_url = f"{nexus_url}/service/rest/v1/assets?repository={repository}"

    if last_sync_date:
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
            response = requests.get(url, auth=(username, password), timeout=timeout, proxies=proxies)
            response.raise_for_status()
            data = response.json()

            current_batch = data.get('items', [])
            assets.extend(current_batch)

            if last_sync_date:
                for asset in current_batch:
                    asset_date = parse_nexus_date(asset.get('lastModified', ''))
                    if asset_date:
                        sync_date = datetime.fromisoformat(last_sync_date.replace('Z', '+00:00')) if isinstance(last_sync_date, str) else last_sync_date
                        if asset_date.tzinfo is None and sync_date.tzinfo is not None:
                            sync_date = sync_date.replace(tzinfo=None)
                        elif asset_date.tzinfo is not None and sync_date.tzinfo is None:
                            asset_date = asset_date.replace(tzinfo=None)

                        if asset_date > sync_date:
                            filtered_assets.append(asset)
                    else:
                        filtered_assets.append(asset)
            else:
                filtered_assets.extend(current_batch)

            logger.info(
                f"Retrieved {len(current_batch)} assets from page {page}" +
                (f" ({len([a for a in current_batch if a in filtered_assets])} new/modified)" if last_sync_date else "")
            )
            continuation_token = data.get('continuationToken')
            if continuation_token and continuation_token != 'None' and page < max_pages:
                url = f"{base_url}&continuationToken={continuation_token}"
                page += 1
            else:
                logger.info(f"Stopping after {page} page(s)")
                break

        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching assets on page {page}: {e}")
            if filtered_assets:
                logger.warning(f"Continuing with {len(filtered_assets)} assets fetched so far")
                break
            raise

    logger.info(f"Total assets found: {len(assets)}")
    if last_sync_date:
        logger.info(f"Assets to sync (modified since last sync): {len(filtered_assets)}")

    return filtered_assets if last_sync_date else assets


def download_asset(asset, download_dir, username=None, password=None, timeout=60, proxies=None):
    """Download an asset from the source Nexus to local storage with authentication."""
    asset_url = asset['downloadUrl']
    asset_path = asset['path']
    sanitized_path = sanitize_filename(asset_path)
    path_parts = [part for part in sanitized_path.split('_') if part and part != '-']
    filename = path_parts[-1] if path_parts else sanitized_path
    local_dir = os.path.join(download_dir, *path_parts[:-1]) if len(path_parts) > 1 else download_dir
    os.makedirs(local_dir, exist_ok=True)
    local_path = os.path.join(local_dir, filename)

    try:
        auth = (username, password) if username and password else None
        with requests.get(asset_url, stream=True, timeout=timeout, auth=auth, proxies=proxies) as r:
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


def upload_npm_package(nexus_url, repository, username, password, local_path, npm_path, timeout=120, proxies=None):
    """Upload NPM package to target Nexus using the correct NPM upload endpoint."""
    upload_url = f"{nexus_url}/service/rest/v1/components?repository={repository}"

    try:
        with open(local_path, 'rb') as file:
            files = {
                'npm.asset': (os.path.basename(local_path), file, 'application/octet-stream')
            }
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
                timeout=timeout,
                proxies=proxies
            )
            response.raise_for_status()
            logger.debug(f"Uploaded: {npm_path} to {upload_url}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error uploading {npm_path}: {e}")
        logger.debug(f"Upload URL: {upload_url}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Server response: {e.response.text}")
        raise


def trigger_proxy_cache(nexus_url, repository, npm_path, username, password, timeout=60, proxies=None, no_proxy=None):
    """Trigger proxy repository to cache the NPM package using npm pack."""
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
    package_spec = f"{package_name}@{package_version}"

    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.npmrc') as npmrc_file:
        registry_url = f"{nexus_url}/repository/{repository}/"
        npmrc_content = (
            f"registry={registry_url}\n"
            f"strict-ssl=false\n"
        )

        if proxies:
            if 'http' in proxies:
                npmrc_content += f"proxy={proxies['http']}\n"
            if 'https' in proxies:
                npmrc_content += f"https-proxy={proxies['https']}\n"
        if no_proxy:
            npmrc_content += f"noproxy={no_proxy}\n"

        npmrc_file.write(npmrc_content)
        npmrc_file_path = npmrc_file.name
        logger.debug(f"Created temporary .npmrc at {npmrc_file_path} with content:\n{npmrc_content}")

    try:
        os.chmod(npmrc_file_path, stat.S_IRUSR | stat.S_IWUSR)
        with open(npmrc_file_path, 'r') as f:
            logger.debug(f"Verified .npmrc content: {f.read()}")
    except OSError as e:
        logger.error(f"Failed to set permissions or read {npmrc_file_path}: {e}")
        raise

    env = os.environ.copy()
    if proxies:
        if 'http' in proxies:
            env['HTTP_PROXY'] = proxies['http']
            env['http_proxy'] = proxies['http']
        if 'https' in proxies:
            env['HTTPS_PROXY'] = proxies['https']
            env['https_proxy'] = proxies['https']
    if no_proxy:
        env['NO_PROXY'] = no_proxy
        env['no_proxy'] = no_proxy

    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            logger.debug(f"Running npm pack for {package_spec} with registry {registry_url}")
            result = subprocess.run(
                ['npm', 'pack', package_spec, '--userconfig', npmrc_file_path, '--pack-destination', temp_dir, '--loglevel', 'verbose', '--registry', registry_url],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env
            )
            result.check_returncode()
            logger.debug(f"npm pack output: {result.stdout}")
            logger.info(f"Successfully triggered cache for {npm_path} on proxy repository")
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
            try:
                os.unlink(npmrc_file_path)
            except OSError as e:
                logger.warning(f"Could not remove temporary .npmrc: {e}")


def migrate_assets_batch(assets, config, proxies=None, no_proxy=None):
    """Legacy batch migration flow based on a full asset list."""
    settings = config['settings']
    source_config = config['source']
    target_config = config['target']

    batch_size = settings.get('batch_size', 10)
    batch_delay = settings.get('batch_delay', 1)

    total_assets = len(assets)
    successful_uploads = 0
    failed_uploads = 0
    synced_assets = []

    repo_type = get_repository_type(
        target_config['nexus_url'],
        target_config['repository'],
        target_config['username'],
        target_config['password'],
        settings.get('request_timeout', 30),
        proxies=proxies
    )

    for i in range(0, total_assets, batch_size):
        batch = assets[i:i + batch_size]
        logger.info(f"Processing batch {i // batch_size + 1}/{(total_assets + batch_size - 1) // batch_size}")

        for asset in batch:
            if not asset['path'].endswith('.tgz'):
                logger.info(f"Skipping directory or non-package asset: {asset['path']}")
                continue

            try:
                logger.info(f"Processing {asset['path']}")
                if repo_type == 'proxy':
                    trigger_proxy_cache(
                        target_config['nexus_url'],
                        target_config['repository'],
                        asset['path'],
                        target_config['username'],
                        target_config['password'],
                        settings.get('download_timeout', 60),
                        proxies=proxies,
                        no_proxy=no_proxy
                    )
                    successful_uploads += 1
                    synced_assets.append({
                        'path': asset['path'],
                        'lastModified': asset.get('lastModified'),
                        'syncedAt': now_utc_iso()
                    })
                    logger.info(f"Successfully triggered cache for: {asset['path']}")
                else:
                    local_path = download_asset(
                        asset,
                        DOWNLOAD_DIR,
                        source_config['username'],
                        source_config['password'],
                        settings.get('download_timeout', 60),
                        proxies=proxies
                    )

                    upload_npm_package(
                        target_config['nexus_url'],
                        target_config['repository'],
                        target_config['username'],
                        target_config['password'],
                        local_path,
                        asset['path'],
                        settings.get('upload_timeout', 120),
                        proxies=proxies
                    )

                    successful_uploads += 1
                    synced_assets.append({
                        'path': asset['path'],
                        'lastModified': asset.get('lastModified'),
                        'syncedAt': now_utc_iso()
                    })
                    logger.info(f"Successfully migrated: {asset['path']}")

                    try:
                        os.remove(local_path)
                    except PermissionError:
                        logger.warning(f"Could not remove {local_path}, will be cleaned up later")

            except Exception as e:
                failed_uploads += 1
                logger.error(f"Failed to {'cache' if repo_type == 'proxy' else 'migrate'} {asset['path']}: {e}")
                continue

        time.sleep(batch_delay)

    return successful_uploads, failed_uploads, synced_assets


def build_package_metadata_url(nexus_url, repository, package_name):
    encoded_name = quote(package_name, safe='@/')
    return f"{nexus_url.rstrip('/')}/repository/{quote(repository)}/{encoded_name}"


def extract_repository_relative_path_from_tarball_url(tarball_url):
    parsed = urlparse(tarball_url)
    marker = '/repository/'
    if marker not in parsed.path:
        return parsed.path

    suffix = parsed.path.split(marker, 1)[1]
    parts = suffix.split('/', 1)
    if len(parts) == 1:
        return '/'
    return '/' + parts[1]


def build_expected_tarball_path(package_name, version, version_metadata=None):
    dist = (version_metadata or {}).get('dist', {})
    tarball_url = dist.get('tarball')
    if tarball_url:
        return extract_repository_relative_path_from_tarball_url(tarball_url)

    package_basename = package_name.split('/')[-1]
    filename = f"{package_basename}-{version}.tgz"
    if package_name.startswith('@'):
        scope, name = package_name.split('/', 1)
        return f"/{scope}/{name}/-/{filename}"
    return f"/{package_name}/-/{filename}"


def build_source_download_url(source_config, path):
    return f"{source_config['nexus_url'].rstrip('/')}/repository/{source_config['repository']}{path}"


def build_source_asset(package_name, version, source_config, version_metadata=None):
    path = build_expected_tarball_path(package_name, version, version_metadata)
    dist = (version_metadata or {}).get('dist', {})
    return {
        'path': path,
        'downloadUrl': dist.get('tarball') or build_source_download_url(source_config, path),
        'lastModified': (version_metadata or {}).get('lastModified'),
    }


def fetch_package_metadata(nexus_url, repository, package_name, username, password, timeout=30, etag=None, proxies=None):
    url = build_package_metadata_url(nexus_url, repository, package_name)
    headers = {'Accept': 'application/json'}
    if etag:
        headers['If-None-Match'] = etag

    response = requests.get(
        url,
        auth=(username, password) if username or password else None,
        headers=headers,
        timeout=timeout,
        proxies=proxies
    )

    if response.status_code == 304:
        return {
            'status': 304,
            'etag': response.headers.get('ETag', etag),
            'last_modified': response.headers.get('Last-Modified'),
            'data': None,
        }

    response.raise_for_status()
    return {
        'status': 200,
        'etag': response.headers.get('ETag'),
        'last_modified': response.headers.get('Last-Modified'),
        'data': response.json(),
    }


def search_assets_by_query(nexus_url, repository, username, password, query, timeout=30, proxies=None, max_pages=10):
    """Search assets by a free-text query and return all matching items."""
    base_url = f"{nexus_url.rstrip('/')}/service/rest/v1/search/assets"
    items = []
    continuation_token = None
    page = 1

    while page <= max_pages:
        params = {
            'repository': repository,
            'q': query,
        }
        if continuation_token:
            params['continuationToken'] = continuation_token

        response = requests.get(
            base_url,
            auth=(username, password) if username or password else None,
            params=params,
            timeout=timeout,
            proxies=proxies,
        )
        response.raise_for_status()
        data = response.json()
        items.extend(data.get('items', []))
        continuation_token = data.get('continuationToken')
        if not continuation_token:
            break
        page += 1

    return items


def target_has_package_version(target_config, package_name, version, expected_path, timeout=30, proxies=None):
    """Check whether the target repository already contains the exact tarball."""
    filename = os.path.basename(expected_path)
    candidate_items = search_assets_by_query(
        target_config['nexus_url'],
        target_config['repository'],
        target_config['username'],
        target_config['password'],
        filename,
        timeout=timeout,
        proxies=proxies,
    )

    for item in candidate_items:
        if item.get('path') == expected_path:
            return True

        npm_data = item.get('npm', {})
        if npm_data.get('name') == package_name and npm_data.get('version') == version:
            return True

    return False


def mark_version_mirrored(package_state, version):
    package_state['mirrored_versions'] = merge_unique(package_state.get('mirrored_versions'), [version])
    package_state['pending_versions'] = sorted(set(package_state.get('pending_versions', [])) - {version})
    package_state['last_synced'] = now_utc_iso()


def mark_version_pending(package_state, version):
    package_state['pending_versions'] = merge_unique(package_state.get('pending_versions'), [version])


def determine_versions_to_process(package_state, metadata_versions, forced_versions=None):
    metadata_versions = set(metadata_versions or [])
    known_versions = set(package_state.get('known_versions', []))
    pending_versions = set(package_state.get('pending_versions', []))
    forced_versions = set(forced_versions or [])

    if forced_versions:
        return sorted(forced_versions | pending_versions)

    if not package_state.get('metadata_initialized') and known_versions:
        return sorted(pending_versions)

    return sorted((metadata_versions - known_versions) | pending_versions)


def sync_package_versions(package_name, versions_to_process, version_map, package_state, config, repo_type, proxies=None, no_proxy=None):
    """Synchronize specific versions for a single npm package."""
    settings = config['settings']
    source_config = config['source']
    target_config = config['target']

    successful = 0
    failed = 0
    skipped_existing = 0
    synced_assets = []

    for version in versions_to_process:
        version_metadata = version_map.get(version, {})
        asset = build_source_asset(package_name, version, source_config, version_metadata)
        expected_path = asset['path']

        try:
            logger.info(f"Existence check: {package_name}@{version} -> {expected_path}")
            if target_has_package_version(
                target_config,
                package_name,
                version,
                expected_path,
                timeout=settings.get('request_timeout', 30),
                proxies=proxies,
            ):
                logger.info(f"Target already has {package_name}@{version}; skipping download/cache warmup")
                skipped_existing += 1
                mark_version_mirrored(package_state, version)
                continue

            logger.info(f"Target missing {package_name}@{version}; synchronizing now")
            if repo_type == 'proxy':
                trigger_proxy_cache(
                    target_config['nexus_url'],
                    target_config['repository'],
                    expected_path,
                    target_config['username'],
                    target_config['password'],
                    settings.get('download_timeout', 60),
                    proxies=proxies,
                    no_proxy=no_proxy,
                )
            else:
                local_path = download_asset(
                    asset,
                    DOWNLOAD_DIR,
                    source_config['username'],
                    source_config['password'],
                    settings.get('download_timeout', 60),
                    proxies=proxies,
                )
                try:
                    upload_npm_package(
                        target_config['nexus_url'],
                        target_config['repository'],
                        target_config['username'],
                        target_config['password'],
                        local_path,
                        expected_path,
                        settings.get('upload_timeout', 120),
                        proxies=proxies,
                    )
                finally:
                    if os.path.exists(local_path):
                        try:
                            os.remove(local_path)
                        except PermissionError:
                            logger.warning(f"Could not remove {local_path}, will be cleaned up later")

            successful += 1
            mark_version_mirrored(package_state, version)
            synced_assets.append({
                'path': expected_path,
                'lastModified': package_state.get('last_modified'),
                'syncedAt': now_utc_iso(),
            })
            logger.info(f"Synchronization succeeded for {package_name}@{version}")
        except Exception as e:
            failed += 1
            mark_version_pending(package_state, version)
            logger.error(f"Failed to synchronize {package_name}@{version}: {e}")

    logger.info(
        f"Package summary {package_name}: {len(versions_to_process)} checked, {successful} synchronized, {skipped_existing} already mirrored, {failed} failed"
    )
    return successful, failed, synced_assets, skipped_existing


def sync_known_packages(sync_state, config, package_names=None, forced_versions_by_package=None, proxies=None, no_proxy=None):
    """Synchronize versions for package names already known to the local state catalog."""
    settings = config['settings']
    source_config = config['source']
    target_config = config['target']
    forced_versions_by_package = forced_versions_by_package or {}

    package_names = sorted(package_names or sync_state['known_packages'].keys())
    if not package_names:
        logger.info("No known packages in sync state. Run with --discover-new to extend the package catalog.")
        sync_state['last_sync_date'] = now_utc_iso()
        sync_state['synced_assets'] = []
        sync_state['total_synced'] = 0
        return 0, 0, []

    repo_type = get_repository_type(
        target_config['nexus_url'],
        target_config['repository'],
        target_config['username'],
        target_config['password'],
        settings.get('request_timeout', 30),
        proxies=proxies,
    )

    total_successful = 0
    total_failed = 0
    total_skipped_existing = 0
    unchanged_packages = 0
    changed_packages = 0
    packages_with_work = 0
    synced_assets = []

    for package_name in package_names:
        package_state = ensure_package_state(sync_state, package_name)
        if package_state.get('deleted'):
            logger.info(f"Skipping deleted package: {package_name}")
            continue
        forced_versions = forced_versions_by_package.get(package_name, [])
        logger.info(f"Checking metadata for package: {package_name}")
        try:
            metadata_response = fetch_package_metadata(
                source_config['nexus_url'],
                source_config['repository'],
                package_name,
                source_config['username'],
                source_config['password'],
                timeout=settings.get('request_timeout', 30),
                etag=package_state.get('etag'),
                proxies=proxies,
            )
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch metadata for {package_name}: {e}")
            continue

        package_state['last_checked'] = now_utc_iso()
        versions_to_process = []
        version_map = {}

        if metadata_response['status'] == 304:
            unchanged_packages += 1
            versions_to_process = determine_versions_to_process(package_state, [], forced_versions)
            logger.info(f"Package metadata unchanged for {package_name}")
        else:
            changed_packages += 1
            metadata = metadata_response['data'] or {}
            version_map = metadata.get('versions', {})
            metadata_versions = set(version_map.keys())
            versions_to_process = determine_versions_to_process(package_state, metadata_versions, forced_versions)
            package_state['etag'] = metadata_response.get('etag') or package_state.get('etag')
            package_state['last_modified'] = metadata_response.get('last_modified')
            package_state['known_versions'] = sorted(metadata_versions)
            package_state['metadata_initialized'] = True
            logger.info(
                f"Package {package_name}: metadata changed, {len(metadata_versions)} known version(s), {len(versions_to_process)} version(s) need verification"
            )

        if not versions_to_process:
            logger.info(f"No version work queued for {package_name}")
            continue

        packages_with_work += 1
        logger.info(
            f"Queued versions for {package_name}: {format_sample_list(versions_to_process)}"
        )
        successful, failed, package_synced_assets, skipped_existing = sync_package_versions(
            package_name,
            versions_to_process,
            version_map,
            package_state,
            config,
            repo_type,
            proxies=proxies,
            no_proxy=no_proxy,
        )
        total_successful += successful
        total_failed += failed
        total_skipped_existing += skipped_existing
        synced_assets.extend(package_synced_assets)

    sync_state['last_sync_date'] = now_utc_iso()
    sync_state['synced_assets'] = synced_assets
    sync_state['total_synced'] = len(synced_assets)
    logger.info(
        "Known-package sync summary: %s package(s) checked, %s metadata unchanged, %s metadata changed, %s package(s) with queued work, %s version(s) synchronized, %s already mirrored, %s failed",
        len(package_names),
        unchanged_packages,
        changed_packages,
        packages_with_work,
        total_successful,
        total_skipped_existing,
        total_failed,
    )
    return total_successful, total_failed, synced_assets


def check_deleted_packages(sync_state, config, proxies=None):
    """Check all known packages against source Nexus and interactively mark 404s as deleted."""
    source_config = config['source']
    target_config = config['target']
    known_packages = sync_state.get('known_packages', {})

    package_names = sorted(known_packages.keys())
    checked = 0
    marked_deleted = 0

    for package_name in package_names:
        package_state = known_packages[package_name]
        if package_state.get('deleted'):
            continue

        checked += 1
        logger.info(f"Checking package: {package_name}")

        try:
            metadata_response = fetch_package_metadata(
                source_config['nexus_url'],
                source_config['repository'],
                package_name,
                source_config['username'],
                source_config['password'],
                timeout=30,
                etag=package_state.get('etag'),
                proxies=proxies,
            )
            # 200 or 304 — package exists
            logger.info(f"  OK (status {metadata_response['status']})")
            continue
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                source_url = build_package_metadata_url(
                    source_config['nexus_url'], source_config['repository'], package_name
                )
                target_url = build_package_metadata_url(
                    target_config['nexus_url'], target_config['repository'], package_name
                )
                versions = package_state.get('known_versions', [])
                print(f"\n  Package NOT FOUND (404): {package_name}")
                print(f"    Known versions : {', '.join(versions) if versions else '(none)'}")
                print(f"    Source URL     : {source_url}")
                print(f"    Target URL     : {target_url}")

                while True:
                    choice = input("  [m]ark as deleted / [s]kip / [q]uit > ").strip().lower()
                    if choice == 'm':
                        package_state['deleted'] = True
                        marked_deleted += 1
                        logger.info(f"  Marked as deleted: {package_name}")
                        break
                    elif choice == 's':
                        logger.info(f"  Skipped: {package_name}")
                        break
                    elif choice == 'q':
                        logger.info("Aborting check-deleted.")
                        print(f"\nSummary: checked {checked} package(s), marked {marked_deleted} as deleted.")
                        return
                    else:
                        print("  Please enter m, s, or q.")
            else:
                logger.error(f"  HTTP error for {package_name}: {e}")
                continue
        except requests.exceptions.RequestException as e:
            logger.error(f"  Request error for {package_name}: {e}")
            continue

    print(f"\nSummary: checked {checked} package(s), marked {marked_deleted} as deleted.")


def collect_new_packages_from_assets(assets, known_package_names):
    """Return newly discovered package names mapped to versions seen since the last discovery scan."""
    new_packages = {}
    for asset in assets:
        path = asset.get('path', '')
        if not path.endswith('.tgz'):
            continue

        package_name = extract_package_name_from_asset(asset)
        version = extract_version_from_asset(asset)
        if not package_name or not version:
            continue

        if package_name in known_package_names:
            continue

        new_packages.setdefault(package_name, set()).add(version)

    return {package_name: sorted(versions) for package_name, versions in new_packages.items()}


def discover_new_packages(sync_state, config, proxies=None):
    """Discover package names that appeared since the last discovery scan."""
    source_config = config['source']
    settings = config['settings']
    last_discovery_date = sync_state.get('last_discovery_date')

    if last_discovery_date:
        logger.info(f"Discovery mode: checking for package names modified since {last_discovery_date}")
    else:
        logger.info("Discovery mode: no previous discovery checkpoint, full package-name scan")

    assets = get_assets(
        source_config['nexus_url'],
        source_config['repository'],
        source_config['username'],
        source_config['password'],
        last_discovery_date,
        settings.get('request_timeout', 30),
        settings.get('max_pages', 1),
        proxies=proxies,
    )

    known_package_names = set(sync_state.get('known_packages', {}).keys())
    discovered = collect_new_packages_from_assets(assets, known_package_names)
    for package_name in discovered:
        ensure_package_state(sync_state, package_name)

    sync_state['last_discovery_date'] = now_utc_iso()
    total_versions = sum(len(versions) for versions in discovered.values())
    logger.info(
        "Discovery summary: %s asset candidate(s) scanned, %s new package name(s), %s new version(s), sample: %s",
        len(assets),
        len(discovered),
        total_versions,
        format_sample_list(sorted(discovered.keys())),
    )
    return discovered


def validate_credentials(config, proxies=None):
    """Validate that both source and target credentials work."""
    source_config = config['source']
    target_config = config['target']
    timeout = config['settings'].get('request_timeout', 30)

    logger.info("Validating source credentials...")
    try:
        response = requests.get(
            f"{source_config['nexus_url']}/service/rest/v1/repositories",
            auth=(source_config['username'], source_config['password']),
            timeout=timeout,
            proxies=proxies
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
            auth=(target_config['username'], target_config['password']) if target_config['username'] or target_config['password'] else None,
            timeout=timeout,
            proxies=proxies
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
            import gc
            gc.collect()
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


def select_cache_invalidation_repositories(repository_items, npm_only=True):
    selected_repositories = []
    for repository in repository_items:
        if repository.get('type', '').lower() != 'proxy':
            continue
        if npm_only and repository.get('format') != 'npm':
            continue
        selected_repositories.append(repository.get('name'))

    return sorted(set([name for name in selected_repositories if name]))


def list_cache_invalidation_repositories(config, proxies=None, npm_only=True):
    target_config = config['target']
    timeout = config['settings'].get('request_timeout', 30)
    response = requests.get(
        f"{target_config['nexus_url']}/service/rest/v1/repositories",
        auth=(target_config['username'], target_config['password']) if target_config['username'] or target_config['password'] else None,
        timeout=timeout,
        proxies=proxies,
    )
    response.raise_for_status()
    return select_cache_invalidation_repositories(response.json(), npm_only=npm_only)


def invalidate_cache_for_repository(target_config, repository_name, timeout=30, proxies=None):
    response = requests.post(
        f"{target_config['nexus_url']}/service/rest/v1/repositories/{repository_name}/invalidate-cache",
        auth=(target_config['username'], target_config['password']) if target_config['username'] or target_config['password'] else None,
        timeout=timeout,
        proxies=proxies,
    )
    response.raise_for_status()


def handle_invalidate_cache(args, config, proxies=None):
    """Invalidate cache for one or more target repositories."""
    target_config = config['target']
    timeout = config['settings'].get('request_timeout', 30)

    if args.all_repos:
        logger.info("Invalidating cache for all accessible npm proxy repositories...")
        try:
            repositories = list_cache_invalidation_repositories(config, proxies=proxies, npm_only=True)
        except requests.exceptions.RequestException as e:
            logger.error(f"Could not list repositories for cache invalidation: {e}")
            return False

        if not repositories:
            logger.info("No npm proxy repositories found for cache invalidation")
            return True

        successful = 0
        failed = 0
        for repository_name in repositories:
            try:
                invalidate_cache_for_repository(target_config, repository_name, timeout=timeout, proxies=proxies)
                successful += 1
                logger.info(f"Cache invalidated successfully for {repository_name}")
            except requests.exceptions.RequestException as e:
                failed += 1
                logger.error(f"Cache invalidation failed for {repository_name}: {e}")

        logger.info(f"Bulk cache invalidation completed: {successful} successful, {failed} failed")
        return failed == 0

    target_repository = args.repo or target_config['repository']
    logger.info(f"Invalidating cache for repository {target_repository}...")
    try:
        invalidate_cache_for_repository(target_config, target_repository, timeout=timeout, proxies=proxies)
        logger.info("Cache invalidated successfully")
    except requests.exceptions.RequestException as e:
        logger.error(f"Cache invalidation failed: {e}")
        return False

    return True


def run_legacy_incremental_sync(config, proxies=None, no_proxy=None):
    """Preserve the original full asset scan flow when no new flags are used."""
    sync_state = load_sync_state()
    last_sync_date = sync_state.get('last_sync_date')

    if last_sync_date:
        logger.info(f"Incremental sync mode: checking for assets modified since {last_sync_date}")
    else:
        logger.info("Full sync mode: no previous sync detected")

    source_config = config['source']
    assets = get_assets(
        source_config['nexus_url'],
        source_config['repository'],
        source_config['username'],
        source_config['password'],
        last_sync_date,
        config['settings'].get('request_timeout', 30),
        config['settings'].get('max_pages', 1),
        proxies=proxies
    )

    if not assets:
        logger.info("No new or modified assets found since last sync")
        return 0, 0

    successful, failed, synced_assets = migrate_assets_batch(assets, config, proxies=proxies, no_proxy=no_proxy)
    if synced_assets:
        sync_state['last_sync_date'] = now_utc_iso()
        sync_state['synced_assets'] = synced_assets
        sync_state['total_synced'] = len(synced_assets)
        save_sync_state(sync_state)

    logger.info(f"Migration completed: {successful} successful, {failed} failed")
    return successful, failed


def main():
    """Main migration function."""
    arg_parser = argparse.ArgumentParser(description="Nexus sync tool")
    arg_parser.add_argument(
        '--sync-known',
        action='store_true',
        help='Synchronize only package names already recorded in the local sync state catalog.'
    )
    arg_parser.add_argument(
        '--discover-new',
        action='store_true',
        help='After known-package sync, discover package names changed since the last discovery checkpoint and process them too.'
    )
    arg_parser.add_argument(
        '--check-deleted',
        action='store_true',
        help='Check all known packages against source Nexus; interactively mark packages returning 404 as deleted.'
    )
    subparsers = arg_parser.add_subparsers(dest='command')
    p_issue = subparsers.add_parser('invalidate-cache')
    p_issue.add_argument('--repo')
    p_issue.add_argument(
        '--all-repos',
        action='store_true',
        help='Invalidate cache for all accessible npm proxy repositories on the target Nexus.'
    )
    args = arg_parser.parse_args()

    logger.info("Starting NPM package migration...")

    config = load_config()
    proxies, no_proxy = get_proxies(config)
    if proxies:
        logger.info(f"Using proxies: {proxies}")
    if no_proxy:
        logger.info(f"Using NO_PROXY: {no_proxy}")

    if args.command == 'invalidate-cache':
        handle_invalidate_cache(args, config, proxies=proxies)
        return

    if args.check_deleted:
        sync_state = load_sync_state()
        check_deleted_packages(sync_state, config, proxies=proxies)
        save_sync_state(sync_state)
        return

    if args.discover_new and not args.sync_known:
        logger.info("--discover-new implies --sync-known; enabling known-package sync first")
        args.sync_known = True

    if not validate_credentials(config, proxies=proxies):
        logger.error("Credential validation failed. Please check your configuration.")
        return

    try:
        if args.sync_known:
            sync_state = load_sync_state()
            logger.info("=== Phase 1/3: Sync Known Packages ===")
            successful, failed, synced_assets = sync_known_packages(sync_state, config, proxies=proxies, no_proxy=no_proxy)

            if args.discover_new:
                logger.info("=== Phase 2/3: Discover New Package Names ===")
                discovered = discover_new_packages(sync_state, config, proxies=proxies)
                if discovered:
                    logger.info(
                        "=== Phase 3/3: Sync Newly Discovered Packages (%s) ===",
                        format_sample_list(sorted(discovered.keys()))
                    )
                    new_successful, new_failed, new_synced_assets = sync_known_packages(
                        sync_state,
                        config,
                        package_names=sorted(discovered.keys()),
                        forced_versions_by_package=discovered,
                        proxies=proxies,
                        no_proxy=no_proxy,
                    )
                    successful += new_successful
                    failed += new_failed
                    synced_assets.extend(new_synced_assets)
                else:
                    logger.info("=== Phase 3/3: No newly discovered packages to sync ===")

            sync_state['synced_assets'] = synced_assets
            sync_state['total_synced'] = len(synced_assets)
            save_sync_state(sync_state)
            logger.info(f"Package-catalog sync completed: {successful} successful, {failed} failed")
        else:
            run_legacy_incremental_sync(config, proxies=proxies, no_proxy=no_proxy)

    except Exception as e:
        logger.error(f"Migration failed: {e}")
        raise

    finally:
        logger.info("Cleaning up temporary files...")
        safe_cleanup(DOWNLOAD_DIR)


if __name__ == '__main__':
    main()
