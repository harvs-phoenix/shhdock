ShhDock Browser Link (Chrome extension)
=======================================

Lets the local ShhDock assistant read and control this browser: read the
open page, Google something, click links, type into boxes, scroll. Every
command travels over http://127.0.0.1:8765 only - nothing leaves the PC.

Install (once)
--------------
1. Open chrome://extensions in Chrome.
2. Turn on "Developer mode" (toggle, top right).
3. Click "Load unpacked" and pick this folder
   (or %LOCALAPPDATA%\ShhDock\chromeext - ShhDock refreshes that copy).
4. Keep "ShhDock Browser Link" enabled.

Then ask ShhDock things like "google the price of the RTX 5060 Ti" or
"what's on the page I have open" - it needs the agent mode set to
Ask first or Full auto in ShhDock settings.

Uninstall
---------
Remove it from the same page, or delete the folder.
