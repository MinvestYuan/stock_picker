# setup-minvest-hook.ps1
# Run this once after cloning stock_picker to install the auto-push hook for Minvest.
# This makes every commit that touches index.html automatically update the public Minvest repo.

$hookDir = ".git/hooks"
$hookFile = "$hookDir/post-commit"

if (-not (Test-Path $hookDir)) {
    New-Item -ItemType Directory -Path $hookDir -Force | Out-Null
}

$hook = @"
#!/bin/sh

# Post-commit hook for stock_picker repo.
# Automatically pushes only index.html to the Minvest remote (git@github.com:HaominYuan/Minvest.git)
# whenever index.html is part of the commit.
# This keeps the Minvest repo containing ONLY index.html.

if git diff --name-only HEAD~1 HEAD 2>/dev/null | grep -q index.html; then
  echo "index.html changed, auto-pushing to Minvest..."
  
  CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
  
  # Create orphan branch with ONLY index.html
  git checkout --orphan minvest-deploy
  git rm -rf . > /dev/null 2>&1 || true
  git checkout "$CURRENT_BRANCH" -- index.html
  git add index.html
  git commit -m "Update Minvest report (auto from stock_picker)"
  
  # Force push to Minvest's main
  git push minvest minvest-deploy:main --force
  
  # Switch back and clean up
  git checkout "$CURRENT_BRANCH"
  git branch -D minvest-deploy
  
  echo "✅ Successfully pushed index.html to Minvest (https://github.com/HaominYuan/Minvest)"
else
  echo "(index.html not changed, skipping auto-push to Minvest)"
fi
"@

# Write with LF only
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($hookFile, $hook, $utf8NoBom)

Write-Output "✅ Minvest post-commit hook installed at $hookFile"
Write-Output "Now every commit touching index.html will auto-update the public Minvest repo."
Write-Output ""
Write-Output "Make sure you have the remote: git remote add minvest git@github.com:HaominYuan/Minvest.git (if not already)"
