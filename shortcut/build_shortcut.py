#!/usr/bin/env python3
"""Builds the 'Send to Claude' iOS Shortcut and signs it so it can be opened on the iPhone.

Flow on the phone: Share sheet (or copied link, when run on its own) -> Send to Claude -> type or dictate a note -> saved as
iCloud Drive/Shortcuts/ReelInbox/reel-<time>.json -> 'Sent to Claude' notification.

    python3 shortcut/build_shortcut.py   # writes shortcut/Send to Claude.shortcut (signed)
"""
import plistlib
import subprocess
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
OBJ = "￼"  # placeholder char Shortcuts uses where a variable sits inside text


def out(uid, name, aggr=None):
    a = {"Type": "ActionOutput", "OutputUUID": uid, "OutputName": name}
    if aggr:
        a["Aggrandizements"] = aggr
    return a


def attachment(value):
    return {"Value": value, "WFSerializationType": "WFTextTokenAttachment"}


def text(*parts):
    """Text made of plain strings and variables (dicts)."""
    s, ranges = "", {}
    for p in parts:
        if isinstance(p, str):
            s += p
        else:
            ranges[f"{{{len(s)}, 1}}"] = p
            s += OBJ
    return {"Value": {"string": s, "attachmentsByRange": ranges}, "WFSerializationType": "WFTextTokenString"}


def action(ident, params):
    return {"WFWorkflowActionIdentifier": ident, "WFWorkflowActionParameters": params}


u_note, u_urls, u_type, u_text, u_clip, u_curls, u_date, u_dict, u_if = (str(uuid.uuid4()).upper() for _ in range(9))
iso = [{"Type": "WFDateFormatVariableAggrandizement", "WFDateFormatStyle": "ISO 8601", "WFISO8601IncludeTime": True}]
stamp = [{"Type": "WFDateFormatVariableAggrandizement", "WFDateFormatStyle": "Custom",
          "WFDateFormat": "yyyyMMdd-HHmmss"}]
link = {"Type": "Variable", "VariableName": "link"}
shared = attachment({"Type": "ExtensionInput"})

# The note prompt comes FIRST and nothing can skip it: whatever Instagram hands over, the sender always
# gets asked for a note, and a file is always saved. The Mac sorts out a missing link (it shows up
# as a failed reel with the reason), and we record what kind of thing was shared so a bad share can
# be diagnosed.
actions = [
    action("is.workflow.actions.ask", {
        "UUID": u_note,
        "WFAskActionPrompt": "Why are you sending this, and for which project?",
        "WFInputType": "Text",
        "WFAllowsMultilineText": True,
    }),
    action("is.workflow.actions.detect.link", {"UUID": u_urls, "WFInput": shared}),
    action("is.workflow.actions.getitemtype", {"UUID": u_type, "WFInput": shared}),
    action("is.workflow.actions.detect.text", {"UUID": u_text, "WFInput": shared}),
    # Link from what was shared; if there is none, fall back to a link on the clipboard.
    action("is.workflow.actions.conditional", {
        "GroupingIdentifier": u_if, "WFControlFlowMode": 0, "WFCondition": 100,
        "WFInput": {"Type": "Variable", "Variable": attachment(out(u_urls, "URLs"))},
    }),
    action("is.workflow.actions.setvariable", {"WFVariableName": "link", "WFInput": attachment(out(u_urls, "URLs"))}),
    action("is.workflow.actions.conditional", {"GroupingIdentifier": u_if, "WFControlFlowMode": 1}),
    action("is.workflow.actions.getclipboard", {"UUID": u_clip}),
    action("is.workflow.actions.detect.link", {"UUID": u_curls, "WFInput": attachment(out(u_clip, "Clipboard"))}),
    action("is.workflow.actions.setvariable", {"WFVariableName": "link", "WFInput": attachment(out(u_curls, "URLs"))}),
    action("is.workflow.actions.conditional", {"GroupingIdentifier": u_if, "WFControlFlowMode": 2}),
    action("is.workflow.actions.date", {"UUID": u_date, "WFDateActionMode": "Current Date"}),
    action("is.workflow.actions.dictionary", {
        "UUID": u_dict,
        "WFItems": {"Value": {"WFDictionaryFieldValueItems": [
            {"WFItemType": 0, "WFKey": text("url"), "WFValue": text(link)},
            {"WFItemType": 0, "WFKey": text("note"), "WFValue": text(out(u_note, "Provided Input"))},
            {"WFItemType": 0, "WFKey": text("sent_at"), "WFValue": text(out(u_date, "Date", iso))},
            {"WFItemType": 0, "WFKey": text("shared_type"), "WFValue": text(out(u_type, "Type"))},
            {"WFItemType": 0, "WFKey": text("shared_text"), "WFValue": text(out(u_text, "Text"))},
            {"WFItemType": 0, "WFKey": text("source"), "WFValue": text("Send to Claude shortcut v2")},
        ]}, "WFSerializationType": "WFDictionaryFieldValue"},
    }),
    action("is.workflow.actions.documentpicker.save", {
        "WFInput": attachment(out(u_dict, "Dictionary")),
        "WFAskWhereToSave": False,
        "WFSaveFileOverwrite": False,
        "WFFileDestinationPath": text("/ReelInbox/reel-", out(u_date, "Date", stamp), ".json"),
    }),
    action("is.workflow.actions.notification", {
        "WFNotificationActionTitle": "Sent to Claude",
        "WFNotificationActionBody": text("Your Mac will pick it up next time it's on."),
        "WFNotificationActionSound": False,
    }),
]

shortcut = {
    "WFWorkflowActions": actions,
    "WFWorkflowClientVersion": "2607.0.2",
    "WFWorkflowMinimumClientVersion": 900,
    "WFWorkflowMinimumClientVersionString": "900",
    "WFWorkflowIcon": {"WFWorkflowIconStartColor": 4282601983, "WFWorkflowIconGlyphNumber": 59511},
    "WFWorkflowImportQuestions": [],
    "WFWorkflowTypes": ["ActionExtension"],
    "WFWorkflowInputContentItemClasses": [
        "WFURLContentItem", "WFStringContentItem", "WFSafariWebPageContentItem", "WFRichTextContentItem",
        "WFArticleContentItem", "WFImageContentItem", "WFAVAssetContentItem", "WFGenericFileContentItem",
    ],
    "WFWorkflowOutputContentItemClasses": [],
    "WFWorkflowHasOutputFallback": False,
    "WFWorkflowHasShortcutInputVariables": True,
    # Run without sharing (Action Button, Back Tap, home-screen icon): use the copied link instead.
    "WFWorkflowNoInputBehavior": {"Name": "WFWorkflowNoInputBehaviorGetClipboard", "Parameters": {}},
    "WFQuickActionSurfaces": [],
}

unsigned = HERE / "Send to Claude.unsigned.shortcut"
signed = HERE / "Send to Claude.shortcut"
unsigned.write_bytes(plistlib.dumps(shortcut, fmt=plistlib.FMT_BINARY))
r = subprocess.run(["shortcuts", "sign", "--mode", "anyone", "--input", str(unsigned), "--output", str(signed)],
                   capture_output=True, text=True)
print(r.stdout, r.stderr, file=sys.stderr)
if r.returncode != 0 or not signed.exists():
    sys.exit(f"signing failed (exit {r.returncode})")
print(signed)
