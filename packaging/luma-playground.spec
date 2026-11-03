# SPDX-License-Identifier: Apache-2.0

Name:           luma-playground
%global tag 0.1.0-alpha.1
Version:        0.1.0
Release:        0.1.alpha1%{?dist}
Summary:        Make an app by describing it, with agents you choose
License:        Apache-2.0 AND ISC
URL:            https://github.com/SimplyLuma/Playground
Source0:        %{url}/archive/v%{tag}/Playground-%{tag}.tar.gz

BuildArch:      noarch
BuildRequires:  python3-devel >= 3.11
BuildRequires:  desktop-file-utils
BuildRequires:  libappstream-glib
# The unit tests are pure Python (core, agents, mcp): no display, no network.
# The vault tests encrypt for real, and project creation runs git.
BuildRequires:  python3-cryptography
BuildRequires:  git-core
# The code view and terminal use Luma's system monospace face.
Requires:       ibm-plex-mono-fonts
# Record turns preview frames into a WebM through GStreamer.
Recommends:     gstreamer1-plugins-good
Requires:       python3 >= 3.11
Requires:       python3-gobject
Requires:       gtk4 >= 4.18
Requires:       libadwaita >= 1.7
Requires:       webkitgtk6.0
Requires:       gtksourceview5
Requires:       vte291-gtk4
Requires:       luma-developer-platform >= 0.1.0-1.luma.74~preview.20260918.1
Requires:       python3-cryptography
Requires:       git
Requires:       openssh-clients
Requires:       bubblewrap
Requires:       google-figtree-fonts
# Claude, Codex and Gemini's ACP adapters run on Node.js 22+. A system Node is used when
# present; otherwise Playground downloads its own pinned copy into the person's data folder.
Recommends:     nodejs
Recommends:     nodejs-npm

%description
Playground is Luma's place to make things with an agent. Say what you want to
make; Playground asks a couple of questions, creates a real project folder
under ~/Documents/Playground Projects with a brief, a plan and an AGENTS.md,
and gets a first version on screen. Ari, Luma's own agent, or the Claude,
Codex, Gemini, OpenCode and Goose programs each run through their own sign-in
over the Agent Client Protocol. Every change arrives with proof; releases go
through a review with a restore point. Servers are reached with short-lived
SSH certificates, and project secrets live in an end-to-end encrypted vault.
It bundles Lucide icon geometry (ISC).

%prep
%autosetup -n Playground-%{tag}

%build
%{python3} -m compileall -q luma_playground

%install
install -d %{buildroot}%{python3_sitelib}
cp -a luma_playground %{buildroot}%{python3_sitelib}/
find %{buildroot}%{python3_sitelib}/luma_playground -name '__pycache__' -prune -exec rm -rf {} +
%{python3} -m compileall -q -d %{python3_sitelib}/luma_playground %{buildroot}%{python3_sitelib}/luma_playground
install -Dm 0755 data/org.projectluma.Playground.in %{buildroot}%{_bindir}/org.projectluma.Playground
# Agents start Playground's MCP server through this; .mcp.json names it by absolute path.
install -Dm 0755 bin/playground-mcp %{buildroot}%{_bindir}/playground-mcp
install -Dm 0644 data/org.projectluma.Playground.desktop %{buildroot}%{_datadir}/applications/org.projectluma.Playground.desktop
install -d %{buildroot}%{_datadir}/dbus-1/services
sed 's|@bindir@|%{_bindir}|g' data/org.projectluma.Playground.service.in \
  >%{buildroot}%{_datadir}/dbus-1/services/org.projectluma.Playground.service
chmod 0644 %{buildroot}%{_datadir}/dbus-1/services/org.projectluma.Playground.service
install -Dm 0644 data/org.projectluma.Playground.metainfo.xml %{buildroot}%{_metainfodir}/org.projectluma.Playground.metainfo.xml
install -Dm 0644 data/org.projectluma.Playground.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/org.projectluma.Playground.svg
# Style sheets, symbolic icons and anything else the app reads at run time.
install -d %{buildroot}%{_datadir}/playground
for sheet in data/*.css; do
  install -m 0644 "$sheet" %{buildroot}%{_datadir}/playground/
done
# The welcome screen draws the app icon from this file, not the icon theme.
install -m 0644 data/org.projectluma.Playground.svg %{buildroot}%{_datadir}/playground/
if [ -d data/icons ]; then cp -a data/icons %{buildroot}%{_datadir}/playground/icons; fi
# Provider marks (Welcome to Playground's ChatGPT row).
if [ -d data/marks ]; then cp -a data/marks %{buildroot}%{_datadir}/playground/marks; fi
desktop-file-validate %{buildroot}%{_datadir}/applications/org.projectluma.Playground.desktop

%check
# D-Bus activation starts the binary this package installed, and no
# template placeholder survives.
grep -Fxq 'Name=org.projectluma.Playground' %{buildroot}%{_datadir}/dbus-1/services/org.projectluma.Playground.service
grep -Fxq 'Exec=%{_bindir}/org.projectluma.Playground --gapplication-service' %{buildroot}%{_datadir}/dbus-1/services/org.projectluma.Playground.service
if grep -Fq '@' %{buildroot}%{_datadir}/dbus-1/services/org.projectluma.Playground.service; then
  echo 'unsubstituted placeholder in org.projectluma.Playground.service' >&2
  exit 1
fi
# What the installed app reads from /usr/share/playground at run time.
for file in playground.css cards.css sheets.css org.projectluma.Playground.svg \
    icons/hicolor/scalable/actions/pg-plus-symbolic.svg icons/hicolor/scalable/actions/pg-chatgpt-symbolic.svg \
    marks/openai.svg; do
  test -s %{buildroot}%{_datadir}/playground/$file
done
# Agents start the MCP server from the path the app writes into .mcp.json.
grep -Fq '"%{_bindir}/playground-mcp"' luma_playground/core/paths.py
# The unit tests: core, agents and the MCP server, with a fake ACP agent and a
# fake OpenAI server on localhost. No display and no network.
# tests/ is not a package: its helpers (fake_acp_agent, sync_support) import by
# bare name, so discovery runs with tests/ as the top level, as DEV.md does.
# The agent fence (bubblewrap) mounts a tmpfs over $HOME, so it has to exist.
mkdir -p .check-home
HOME=$PWD/.check-home PYTHONPATH=$PWD %{python3} -m unittest discover -s tests -v
appstream-util validate-relax --nonet data/org.projectluma.Playground.metainfo.xml

%files
%license LICENSE data/icons/LUCIDE-LICENSE
%{_bindir}/org.projectluma.Playground
%{_bindir}/playground-mcp
%{python3_sitelib}/luma_playground/
%{_datadir}/applications/org.projectluma.Playground.desktop
%{_datadir}/dbus-1/services/org.projectluma.Playground.service
%{_metainfodir}/org.projectluma.Playground.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/org.projectluma.Playground.svg
%{_datadir}/playground/

%changelog
* Tue Nov 03 2026 Project Luma <maintainers@projectluma.org> - 0.1.0-0.1.alpha1
- First alpha
