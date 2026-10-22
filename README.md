# Wallpaper approval

Review Wallhaven images locally, publish approved image links, and sync approved wallpapers to another computer.

## Run the review app

```sh
python3 -m pip install -r requirements.txt
python3 review.py
```

Use **Find** to discover safe-for-work links. Search options include comma-separated topics, resolution, popularity, sorting, and result count. Finding images saves the complete Wallhaven search result and wallpaper detail response in `catalog.sqlite`; it does not download wallpaper files. Use **Fetch saved metadata** to fill in the detail response for older links.

## Review images

- Click a thumbnail to preview the full image.
- Press **A** to approve or **R** to reject.
- Select thumbnails to batch approve, reject, restore, or clean.
- Clean moves downloaded files to Trash and removes link-only previews.
- Approved images are saved in `approved/`.
- Use **Sync approved files** in the review toolbar to download missing approved images and remove files that are no longer approved. It uses local approvals on the review machine and keeps using `approved-links.sqlite` on receiving machines after their first bundle sync.
- Preview controls are above the image; **Show thumbnails** opens a strip below it.
- Filter by comma-separated Wallhaven tags; matches must include every listed tag.
- Filter choices and the selected tab persist in this browser.

Review images before using them at work. In KDE, set a slideshow using the `approved/` folder.

## Publish updated approvals

After changing approvals, refresh and stage the shareable link bundle:

```sh
./update-approved.sh
```

Commit and push the staged `approved-links.sqlite` to publish it. The bundle contains approved links and metadata, not wallpaper image files. The full local `catalog.sqlite` stays private and should not be shared between computers.

## Sync on another computer

The receiving computer needs `curl` and the `sqlite3` command-line tool. It does not need Python.
Sync downloads newly approved images and removes local `approved/` wallpapers that are no longer in the bundle. Cleanup runs only when every bundle row is valid and all required downloads succeed; any failure keeps the existing local images for that run.

To sync a bundle manually:

```sh
sh /path/to/wallpaper/sync_approved.sh /path/to/approved-links.sqlite [wallpaper-folder]
```

To install from a hosted repository, first set `BASE_URL` in `install.sh` to the HTTPS raw URL of the folder containing the sync scripts and `approved-links.sqlite`. Then run:

```sh
curl -fsSL https://raw.githubusercontent.com/OWNER/REPO/main/install.sh | sh
```

The installer asks for a wallpaper folder (default `./wallpapers`), syncs immediately, then offers daily, weekly, or hourly cron updates. Uninstall with:

```sh
sh /path/to/wallpapers/.wallpaper-sync/uninstall.sh
```

Uninstall removes the cron job and helper files, but keeps downloaded wallpapers.

Wallhaven images belong to their respective owners. Check an image's source page before redistributing it.
