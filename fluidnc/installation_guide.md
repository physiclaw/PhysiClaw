# FluidNC Installation Guide

MKS DLC32 v2.1 — CoreXY + Solenoid

---

## Before You Start

Make sure you have the following ready:

- MKS DLC32 v2.1 board
- USB Type-B cable (the square connector, same as a printer cable)
- 12V power supply
- Chrome browser
- The provided `config.yaml` file

---

## Step 1 — Power up the board

Connect 12V power to the board. The LED will light up. Then plug in USB.

## Step 2 — Open the installer

Open [fluidnc installer](https://installer.fluidnc.com) in Chrome. The page shows two products — click **Continue** under **FluidNC**.

## Step 3 — Connect to the board

Click **Connect**. A dialog lists available serial ports. Select **USB Serial** and click **Connect**.

## Step 4 — Go to Install

The Home page appears. Click **Install** in the left sidebar.

## Step 5 — Select firmware variant

Select the latest version from the dropdown. Three firmware variants appear — click **noradio**.

## Step 6 — Select installation type

Click **fresh-install**.

## Step 7 — Confirm installation

A confirmation dialog appears. Make sure **Installation speed** is **115200 baud**. Click **Install**.

## Step 8 — Wait for installation

A progress bar appears. Wait until it completes.

## Step 9 — Installation complete

A "Done" dialog confirms the installation. Click **Continue**.

## Step 10 — Open File browser

The Home page shows a red warning about an invalid configuration — this is expected, the board has no config file yet. Click **File browser** in the left sidebar.

## Step 11 — Create config file

The File browser shows a prompt asking to create a config file. Click **Create config**. A dialog appears with `config.yaml` pre-filled as the filename. Click **OK**.

## Step 12 — Open the Source tab

The editor opens on the **General** tab. Click the **Source** tab (rightmost).

## Step 13 — Paste the config

Select all the existing text and delete it. Paste your `config.yaml` content.

## Step 14 — Save

Click **Save**.

## Step 15 — Go to Terminal

Click **Terminal** in the left sidebar.

## Step 16 — Restart the board

Click the red **Restart** button. The board reboots and prints the startup log.

## Step 17 — Verify

All messages should be green. The last line should read `<Idle|...>`. Setup is complete.
