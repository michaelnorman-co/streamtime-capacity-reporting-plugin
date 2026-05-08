/**
 * Capacity refresh button — triggers the GitHub Actions workflow that updates this sheet.
 *
 * Setup (one time):
 *   1. Sheet → Extensions → Apps Script → paste this file's contents into Code.gs
 *   2. In Apps Script: Project Settings → Script properties → add property:
 *        GITHUB_TOKEN = <fine-grained PAT, see README>
 *   3. Save & reload the sheet.
 *
 * Usage:
 *   - Custom menu "Capacity" appears at the top of the sheet on open.
 *   - "Refresh from StreamTime" → kicks off the GitHub Actions run.
 *   - Wait ~30-60 seconds, then refresh the sheet to see new data.
 *   - You can also assign `refreshCapacity` to a drawing-as-button (Insert → Drawing → save → click → ⋮ → Assign script → "refreshCapacity").
 */

const REPO_OWNER = "ragged-edge-design";
const REPO_NAME = "streamtime-capacity-reporting-plugin";
const WORKFLOW_FILE = "weekly-capacity.yml";
const BRANCH = "main";

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu("Capacity")
    .addItem("Refresh from StreamTime", "refreshCapacity")
    .addItem("View latest run on GitHub", "openLatestRun")
    .addToUi();
}

function refreshCapacity() {
  const token = PropertiesService.getScriptProperties().getProperty("GITHUB_TOKEN");
  if (!token) {
    SpreadsheetApp.getUi().alert(
      "GitHub token not configured.\n\n" +
      "Project Settings → Script properties → add GITHUB_TOKEN."
    );
    return;
  }

  const url = `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/actions/workflows/${WORKFLOW_FILE}/dispatches`;
  const resp = UrlFetchApp.fetch(url, {
    method: "post",
    contentType: "application/json",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    payload: JSON.stringify({ ref: BRANCH, inputs: {} }),
    muteHttpExceptions: true,
  });

  const code = resp.getResponseCode();
  if (code === 204) {
    SpreadsheetApp.getActiveSpreadsheet().toast(
      "Started — your sheet will refresh in ~30-60 seconds. Reload the tab to see new numbers.",
      "Capacity refresh kicked off",
      8
    );
  } else {
    SpreadsheetApp.getUi().alert(
      `GitHub returned ${code}.\n\n${resp.getContentText()}`
    );
  }
}

function openLatestRun() {
  const url = `https://github.com/${REPO_OWNER}/${REPO_NAME}/actions/workflows/${WORKFLOW_FILE}`;
  const html = HtmlService.createHtmlOutput(
    `<script>window.open("${url}", "_blank"); google.script.host.close();</script>`
  ).setWidth(10).setHeight(10);
  SpreadsheetApp.getUi().showModalDialog(html, "Opening GitHub…");
}
