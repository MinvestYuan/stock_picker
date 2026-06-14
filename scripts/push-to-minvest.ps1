# scripts/push-to-minvest.ps1
# This script is called by the post-commit hook.
# It checks if index.html changed in the last commit and pushes only it to the Minvest remote.

$repoRoot = git rev-parse --show-toplevel
Set-Location $repoRoot

# Check if index.html was modified in the last commit
$changedFiles = git diff --name-only HEAD~1 HEAD 2>$null
if ($changedFiles -notcontains "index.html") {
    Write-Host "(index.html not changed in this commit, skipping Minvest push)"
    exit 0
}

Write-Host "index.html changed in this commit, auto-pushing to Minvest..."

$branch = git rev-parse --abbrev-ref HEAD

# Use orphan branch to create a commit with ONLY index.html
git checkout --orphan minvest-deploy 2>$null
git rm -rf . 2>$null | Out-Null
git checkout $branch -- index.html
git add index.html
git commit -m "Update Minvest report (auto from stock_picker)"

# Force push to minvest remote's main
git push minvest minvest-deploy:main --force

# Switch back
git checkout $branch
git branch -D minvest-deploy

Write-Host "✅ Successfully pushed index.html to Minvest (git@github.com:HaominYuan/Minvest.git)"
