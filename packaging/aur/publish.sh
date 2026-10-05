#!/usr/bin/env bash
# Push this PKGBUILD to the AUR (maintainer tool).
#
# One-time setup: create an account at https://aur.archlinux.org and add your SSH
# public key in "My Account". If that key isn't your default one, pass it:
#   AUR_SSH_KEY=~/.ssh/id_ed25519_aur ./publish.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

pkg=screenshooter
if [[ -n ${AUR_SSH_KEY:-} ]]; then
    export GIT_SSH_COMMAND="ssh -i $AUR_SSH_KEY -o IdentitiesOnly=yes"
fi

makepkg --printsrcinfo > .SRCINFO
ver=$(sed -n 's/^\tpkgver = //p' .SRCINFO)-$(sed -n 's/^\tpkgrel = //p' .SRCINFO)

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git clone "ssh://aur@aur.archlinux.org/$pkg.git" "$work/$pkg"
cp PKGBUILD .SRCINFO "$pkg.install" "$work/$pkg/"
cd "$work/$pkg"
git add PKGBUILD .SRCINFO "$pkg.install"
if git diff --cached --quiet; then
    echo "AUR already has $ver, nothing to push."
    exit 0
fi
git commit -m "Update to $ver"
git push origin HEAD:master
echo "Published $pkg $ver: https://aur.archlinux.org/packages/$pkg"
