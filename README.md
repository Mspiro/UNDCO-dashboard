# UNDCO Dev Dashboard

A local control panel for the `undg-country` project. One browser page, instead of multiple terminals, to:

- start, stop and restart DDEV
- run theme **Watch**, **Build** and **Storybook** for each site
- get a one-time login link (**ULI**)
- **Update Site** (database updates + config import + cache rebuild) with safety checks
- **Clear Cache**
- read every process's log in one place

It's a single Python file with no packages to install.

| Site | Theme | Storybook |
|---|---|---|
| UNDCO | `undco_theme` (base) | :6006 |
| UNCT | `unct` | :6009 |
| UNSDG | `sdg` | :6008 |

---

## Requirements

| Needed | Notes |
|---|---|
| **Python 3.8+** | Standard library only, nothing to `pip install`. |
| **git** | Used for the branch name and the Update Site safety checks. |
| **DDEV** | With `undg-country` set up as usual (drush aliases `@undco.local`, `@unct.local`, `@unsdg.local`). |
| **Node 24** | Through [nvm](https://github.com/nvm-sh/nvm) (recommended; the dashboard switches to the theme's `.nvmrc` for you), or Node 24 as your system Node. |
| **Linux or macOS** | Windows works only inside WSL. |

You don't need to install theme `node_modules` first. The dashboard runs `npm ci` when they're missing.

---

## Setup

1. Make a folder **next to** your project and put `dashboard.py` in it:

   ```
   Projects/
   ├── undg-country/        ← the Drupal project
   └── undco-dashboard/     ← this folder
       └── dashboard.py
   ```

2. Run it:

   ```bash
   cd undco-dashboard
   python3 dashboard.py
   ```

3. Your browser opens the dashboard at `http://localhost:8800`.

The dashboard finds the project folder by itself when it sits next to it like this. If yours lives elsewhere, either:

- answer the question in the terminal once (the answer is remembered), or
- pass the path: `python3 dashboard.py ~/path/to/undg-country`

Settings (the project path and light/dark theme) are saved in `config.json` next to the script. It's created on first run and is git-ignored.

**To stop it,** press **Ctrl+C** in its terminal. That also stops every Watch and Storybook it started. If an **Update Site** is running, it waits for it to finish first.

---

## Using it

### DDEV
**Start**, **Stop** and **Restart**. The badge shows **Running**, **Spinning up…** or **Stopped**. It also notices when DDEV was started or stopped from a terminal.

### Site cards
| Button | What it does |
|---|---|
| **ULI** | Creates a one-time login link. **Copy** or **Browse** it. The link disappears once you've used it; click ULI again for a new one. |
| **Watch** | `npm run watch` for the site's theme. UNCT and UNSDG also start the `undco_theme` watch (shared, only one runs). The UNDCO button then shows **Used By UNCT** and is locked until the sites using it stop. |
| **Build** | `npm run build`. Builds `undco_theme` first, unless its watch is already running. |
| **Storybook** | Starts the theme's Storybook. The button shows **Starting…** until it's ready, then the **↗** button opens it. |
| **Update Site** | `drush deploy` (updb → cim → cr → deploy hooks). See the safety checks below. |
| **Clear Cache** | `drush cr` |

### Logs
Each running or finished task gets its own tab, with terminal colours. A green dot means it's still running.

- A tab closes by itself when its task **succeeds**, or when you stop it.
- A tab **stays open when something fails**, so you can read the error. **Copy Log** copies the active tab as plain text.

### Update Site safety checks
Before anything changes, the dashboard checks that:

1. DDEV is running.
2. This site's config (`config/<site>/` and `config/shared/`) has no uncommitted changes. If it has, it lists them. Cancel if they're left over from a config export; OK if they're your own work on this branch.
3. Config import preview (`cim --no`): it shows how many items would be created, updated or deleted (⚠ when there are deletes), and you confirm.
4. config_split is set up. If it isn't, which is typical right after importing a prod dump, it blocks the update and shows the commands to fix it.
5. The branch and config haven't changed between the preview and your OK.

There is deliberately **no config export button**. With config_split, exporting writes into the repo's split folders.

### Branch changes
Switch branch (or `git pull`) as normal while the dashboard runs. Within a few seconds it:

- runs `npm ci` for any theme whose `package-lock.json` changed,
- restarts running Watch and Storybook processes, so new components are picked up,
- shows a banner reminding you to click **Update Site** on the sites you use.

The current branch is always shown in the header.

### Other things
- **Already running processes** (a `npm run watch` or Storybook you started in a terminal) are taken over at startup: stopped and restarted from the dashboard, so their logs show here. Linux only.
- **Restarting the script** reuses your open browser tab; it reloads itself.
- **Port 8800 busy?** The next free port is used, skipping DDEV and Storybook ports. The terminal prints the address.
- **Light and dark** follow your system; click **◐** to switch. Your choice is remembered.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Cannot find package …` in a Watch/Build log | The theme's packages are out of date. Delete that theme's `node_modules`; the next click reinstalls it. |
| Log's first line shows `node v20` (or anything but 24) | Install nvm, or switch your system Node to 24. |
| ULI says `uli failed` | DDEV isn't running, or that site's database isn't installed locally. |
| Storybook stays on **Starting…** | Open its log tab. Usually the port is taken by a Storybook running elsewhere. |
| Update Site: "config/ has uncommitted changes" | Run `git status config/`. Restore anything you didn't change on purpose (`git restore -- config/`). |
| Page shows "Dashboard stopped" | The script isn't running. Start it again and the tab reconnects. |
