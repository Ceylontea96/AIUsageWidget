# Read-only publication preflight. Dot-sourcing this file never publishes/builds.
function Assert-NewReleaseVersion {
    param([string]$Version, [string[]]$PublishedVersions)
    if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "Invalid target version: $Version" }
    foreach ($published in $PublishedVersions) {
        $normalized = $published -replace '^v', ''
        if ($normalized -notmatch '^\d+\.\d+\.\d+$') {
            throw "Cannot validate published version: $published"
        }
        if ([version]$Version -le [version]$normalized) {
            throw "Publish rejected: $Version must be newer than $published. Existing artifacts cannot be overwritten."
        }
    }
}

function Assert-PublishAllowed {
    param([string]$Version, [string]$Feed, [string]$GitHubExecutable, [string]$Repository)
    Assert-NewReleaseVersion -Version $Version -PublishedVersions @()
    if ($Feed) {
        if (-not $Feed.StartsWith('https://')) { throw 'Publish feed must use HTTPS' }
        # Failure to verify is not evidence that a release does not exist.
        $latest = Invoke-RestMethod -Uri $Feed -UseBasicParsing -TimeoutSec 20 -ErrorAction Stop
        if (-not $latest.version) { throw 'Remote latest.json has no version' }
        Assert-NewReleaseVersion -Version $Version -PublishedVersions @([string]$latest.version)
    }
    if ($GitHubExecutable) {
        $tags = @(& $GitHubExecutable api --paginate "repos/$Repository/releases" --jq '.[].tag_name')
        if ($LASTEXITCODE -ne 0) { throw 'Cannot verify existing GitHub releases; publish aborted' }
        Assert-NewReleaseVersion -Version $Version -PublishedVersions $tags
    }
}
