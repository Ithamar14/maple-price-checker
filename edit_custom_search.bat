@echo off
rem Opens your custom search list (custom_search.txt) in Notepad.
rem Items on it get a cyan box whenever they're listed. Restart the price checker / crawler after saving.
cd /d "%~dp0"
if not exist custom_search.txt (
  > custom_search.txt (
    echo # Custom search: items to point out ^(cyan box^) whenever they're listed, for any reason.
    echo # One item name per line, as the game spells it; capitals and spaces don't matter.
    echo # Lines starting with # are ignored. Save the file, then restart the price checker / crawler.
    echo #
    echo # Examples ^(remove the # to use them^):
    echo # Work Gloves
    echo # Hat Accuracy Scroll: Greater
  )
)
start "" notepad custom_search.txt
