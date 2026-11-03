# SPDX-License-Identifier: Apache-2.0
# Playground for Fedora without Luma's platform: carries its own build of LumaUI under /usr/lib/playground.
# On Luma, install luma-playground instead (../luma-playground.spec), which uses the system's LumaUI.

%global tag 0.1.0-alpha.2
# The LumaUI source it builds: src/luma-platform, LumaUI's Lucide glyphs (ISC) from assets/icon-theme/Prairie and
# Figtree (OFL-1.1) from assets/fonts/figtree with scripts/fonts/adjust-figtree-ui-metrics.py, of
# https://github.com/SimplyLuma/luma at this commit, under luma/.
%global lumaui 5a836fd

Name:           playground
Version:        0.1.0
Release:        0.2.alpha2%{?dist}
Summary:        Make an app by describing it, with agents you choose
License:        Apache-2.0 AND ISC AND MIT AND OFL-1.1
URL:            https://github.com/SimplyLuma/Playground
Source0:        %{url}/archive/v%{tag}/Playground-%{tag}.tar.gz
Source1:        lumaui-%{lumaui}.tar.gz

BuildRequires:  gcc
BuildRequires:  meson
BuildRequires:  ninja-build
BuildRequires:  glib2-devel >= 2.80
BuildRequires:  gobject-introspection-devel
BuildRequires:  gtk4-devel >= 4.18
BuildRequires:  libadwaita-devel >= 1.7
BuildRequires:  wayland-devel
BuildRequires:  python3-devel
BuildRequires:  desktop-file-utils
BuildRequires:  python3-fonttools
Requires:       python3 >= 3.11
Requires:       python3-gobject
Requires:       gtk4 >= 4.18
Requires:       libadwaita >= 1.7
Requires:       webkitgtk6.0
Requires:       gtksourceview5
Requires:       vte291-gtk4
Requires:       adwaita-icon-theme
Requires:       python3-cryptography
Requires:       git
Requires:       openssh-clients
Requires:       bubblewrap
# Previews of desktop apps (gtk4-broadwayd), project boxes, and agent adapters.
Recommends:     gtk4-devel-tools
Recommends:     podman
Recommends:     nodejs
Conflicts:      luma-playground

%description
Playground is an agentic development environment. Say what you want to make;
it creates a real project folder, connects an agent of your choice, and shows
the running app in a live preview at phone, tablet and desktop sizes. This
build carries its own copy of LumaUI, Luma's toolkit, for systems without Luma.

%prep
%autosetup -n Playground-%{tag}
tar -xzf %{SOURCE1}

%build
# stage.sh builds LumaUI and lays everything out (packaging/linux/stage.sh).

%install
BUILD_DIR=%{_builddir}/lumaui-build sh packaging/linux/stage.sh "$PWD" "$PWD/luma/src/luma-platform" %{buildroot}
desktop-file-validate %{buildroot}%{_datadir}/applications/org.projectluma.Playground.desktop

%files
%license LICENSE data/icons/LUCIDE-LICENSE
%{_bindir}/playground
%{_prefix}/lib/playground/
%{_datadir}/applications/org.projectluma.Playground.desktop
%{_datadir}/icons/hicolor/scalable/apps/org.projectluma.Playground.svg
%{_metainfodir}/org.projectluma.Playground.metainfo.xml

%changelog
* Tue Nov 03 2026 Project Luma <maintainers@projectluma.org> - 0.1.0-0.2.alpha2
- Builds for Fedora, Debian and Ubuntu with their own LumaUI; previewed apps keep its library paths

* Tue Nov 03 2026 Project Luma <maintainers@projectluma.org> - 0.1.0-0.1.alpha1
- First alpha for Fedora without Luma
