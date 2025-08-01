# NexusSync: NPM Package Synchronization Tool

## Overview

`nexusync.py` is a Python script designed to synchronize NPM packages between two Nexus repositories. It supports:
- **Hosted Repositories**: Downloads packages from a source Nexus and uploads them to a target hosted Nexus.
- **Proxy Repositories**: Triggers caching of packages in a target proxy Nexus using `npm pack`.
- **Incremental Sync**: Only processes packages modified since the last sync, based on a stored state file.

The script handles scoped and non-scoped NPM packages, sanitizes filenames for safe storage, and processes assets in batches to avoid overwhelming servers.

## Requirements

- **Python**: 3.6 or higher
- **Dependencies**:
  - `requests`
  - `shutil`
  - `logging`
  - `pathlib`
  - `json`
  - `datetime`
  - `re`
  - `base64`
  - `subprocess`
  - `tempfile`
  - `stat`
- **External Tools**:
  - `npm` (Node Package Manager) installed and accessible for proxy repository caching.
- **Operating System**: Compatible with Windows, Linux, or macOS.

Install dependencies:
```bash
pip install requests
```

## Setup

1. **Clone or Download the Script**:
   - Place `nexusync.py` in your working directory.

2. **Configure the Script**:
   - The script uses a configuration file (`nexus_sync_config.json`) to specify source and target Nexus details.
   - If the file doesn’t exist, a default template is created on first run:
     ```json
     {
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
             "max_pages": 1000
         }
     }
     ```
   - Update `nexus_sync_config.json` with your Nexus URLs, repository names, and credentials.
     - **Source**: The Nexus repository to fetch packages from (e.g., `https://<nexus>`, `<reponame>`).
     - **Target**: The Nexus repository to sync to (e.g., `http://<targetnexus>`, `<reponame>`).
     - **Credentials**: Provide usernames and passwords. For unauthenticated servers, leave `username` and `password` as empty strings (`""`).
     - **Settings**:
       - `batch_size`: Number of packages processed per batch.
       - `download_timeout`: Timeout (seconds) for downloading assets.
       - `upload_timeout`: Timeout (seconds) for uploading assets.
       - `request_timeout`: Timeout (seconds) for API requests.
       - `batch_delay`: Delay (seconds) between batches.
       - `max_pages`: Maximum pages of assets to fetch from the source Nexus.

3. **Ensure npm is Installed** (for proxy repositories):
   - Verify npm is accessible:
     ```bash
     npm --version
     ```
   - Install Node.js/npm if needed: [Node.js Download](https://nodejs.org/).

## Usage

1. **Run the Script**:
   ```bash
   python nexusync.py
   ```
   - The script loads the configuration from `nexus_sync_config.json`.
   - It checks the last sync state (`nexus_sync_state.json`) for incremental sync.
   - It fetches assets from the source Nexus, downloads/uploads (for hosted) or triggers caching (for proxy), and saves the sync state.

2. **Output**:
   - Logs are printed to the console with timestamps and levels (`INFO`, `DEBUG`, `ERROR`).
   - Temporary files are stored in `./downloaded_assets` (cleaned up after successful uploads).
   - Sync state is saved to `nexus_sync_state.json`.

3. **Example**:
   - To sync `<package>` from `https://<nexus>/repository/<reponame>` to a proxy at `http://<nexus>/repository/<reponame>`:
     - Ensure `nexus_sync_config.json` is configured.
     - Run: `python nexusync.py`
     - Check logs for success or errors.

## Features

- **Incremental Sync**: Only processes assets modified since the last sync, using `nexus_sync_state.json`.
- **Batch Processing**: Handles assets in configurable batches to prevent server overload.
- **Filename Sanitization**: Ensures safe filenames for local storage.
- **Authentication Support**: Handles authenticated and unauthenticated Nexus repositories.
- **Error Handling**: Logs detailed errors for debugging and continues processing on partial failures.
- **Cleanup**: Safely removes temporary files with retry logic for Windows.

## Troubleshooting

1. **Version Parsing Errors**:
   - If logs show incorrect package specs (e.g., `@npm/sdk@1.5.0` instead of `@npm/sdk@1.5.0-dev.1111`):
     - Verify the `trigger_proxy_cache` and `upload_npm_package` functions use the latest version parsing logic (see script comments).
     - Test manually:
       ```bash
       npm pack @npm/sdk@1.5.0-dev.1111 --registry http://<nexus>/repository/<reponame>/ --loglevel verbose
       ```

2. **npm Pack Errors** (e.g., `non-zero exit status`):
   - Check `npm pack output` in logs for details (e.g., `404`, `403`).
   - Verify the package exists in the source:
     ```bash
     curl -u <source_username>:<source_password> \
          https://<source_nexus>/repository/<source_repo>/@npm/sdk/-/sdk-1.5.0-dev.11111.tgz
     ```
   - Ensure the proxy’s **Remote Storage** is set to the source Nexus.

3. **Configuration Issues**:
   - If `nexus_sync_config.json` is missing or invalid, a default is created. Update it with correct details.
   - Ensure target credentials are correct or empty (`""`) for unauthenticated servers.

4. **Logs**:
   - Enable debug logging by changing `logging.basicConfig(level=logging.INFO)` to `logging.DEBUG` in the script.
   - Share full logs, including `npm pack output`, for further assistance.

## Example Configuration

```json
{
    "source": {
        "nexus_url": "https://<sourcenexus>",
        "repository": "<reponame>",
        "username": "<login>",
        "password": "<pass>"
    },
    "target": {
        "nexus_url": "http://<nexus>",
        "repository": "<reponame>",
        "username": "",
        "password": ""
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
```

## Notes

- **Proxy Repositories**: Ensure the target proxy’s **Remote Storage** points to the source Nexus.
- **Performance**: Adjust `batch_size` and `max_pages` for large repositories to balance speed and server load.
- **Windows Cleanup**: The script includes retry logic for file cleanup due to Windows file handle issues.

## License

GNU General Public License v3.0

## Contact

For issues or contributions, contact your repository administrator or open an issue in the project repository (if applicable).