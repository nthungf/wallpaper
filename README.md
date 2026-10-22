# Wallpaper approval

Review Wallhaven images locally, publish approved image links, and sync approved wallpapers to another computer.

Repository: [github.com/nthungf/wallpaper](https://github.com/nthungf/wallpaper) (`git@github.com:nthungf/wallpaper.git`).

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

The receiving computer needs `curl` and the `sqlite3` command-line tool. It does not need Python. Run this there:

```sh
curl -fsSL https://raw.githubusercontent.com/nthungf/wallpaper/main/install.sh | sh
```

The installer prompts for a wallpaper folder (default: `./wallpapers`), syncs immediately, then offers daily, weekly, or hourly updates. It downloads the SQLite bundle temporarily and removes it after syncing. Only approved wallpaper images remain in the selected folder. Cleanup removes outdated images only after every bundle row is valid and all required downloads succeed.

To remove an automatic sync, run `crontab -e` and delete the line marked `# wallpaper-sync-managed`. Downloaded wallpapers stay in the folder.

Wallhaven images belong to their respective owners. Check an image's source page before redistributing it.
