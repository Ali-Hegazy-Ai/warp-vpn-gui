.PHONY: all run build appimage deb source clean install uninstall lint security

APP = warp_gui.py
BINARY = dist/warp-gui
APPIMAGE = Cloudflare_WARP_VPN-x86_64.AppImage
VERSION ?= $(shell sh -c 'tag=$$(git describe --tags --abbrev=0 2>/dev/null || true); tag=$${tag#v}; if [ -n "$$tag" ]; then echo "$$tag"; else echo 0.0.0; fi')
DEB = dist/cloudflare-warp-vpn-gui_$(VERSION)_amd64.deb
SRC_ARCHIVE = dist/warp-vpn-gui-$(VERSION).tar.gz
DESKTOP = warp-vpn.desktop
ICON_NAME = warp-vpn
ICON_SRC = /usr/share/icons/gnome/48x48/devices/network-vpn.png
ICON_DST = $(HOME)/.local/share/icons/hicolor/48x48/apps/$(ICON_NAME).png

all: build

run:
	python3 $(APP)

build: $(BINARY)

$(BINARY): $(APP)
	./build.sh

appimage: $(BINARY)
	mkdir -p AppDir/usr/bin AppDir/usr/share/applications \
	         AppDir/usr/share/icons/hicolor/48x48/apps
	cp $(BINARY) AppDir/usr/bin/
	python3 tools/gen_icon.py \
	    AppDir/usr/share/icons/hicolor/48x48/apps/warp-vpn.png
	sed 's|Exec=.*|Exec=warp-gui|; s|Icon=.*|Icon=warp-vpn|' \
	    $(DESKTOP) > AppDir/usr/share/applications/warp-vpn.desktop
	cp AppDir/usr/share/applications/warp-vpn.desktop AppDir/
	cp AppDir/usr/share/icons/hicolor/48x48/apps/warp-vpn.png AppDir/
	printf '#!/bin/bash\nHERE="$$(dirname "$$(readlink -f "$$0")")"\nexec "$$HERE/usr/bin/warp-gui" "$$@"\n' \
	    > AppDir/AppRun
	chmod +x AppDir/AppRun
	ARCH=x86_64 appimagetool AppDir $(APPIMAGE)

deb: $(BINARY)
	./tools/build_deb.sh "$(VERSION)" "$(DEB)"

source:
	mkdir -p dist
	git archive --format=tar.gz \
		--prefix=warp-vpn-gui-$(VERSION)/ \
		-o "$(SRC_ARCHIVE)" \
		HEAD

install: $(DESKTOP) $(ICON_DST)
	mkdir -p $(HOME)/.local/share/applications
	cp $(DESKTOP) $(HOME)/.local/share/applications/
	gtk-update-icon-cache $(HOME)/.local/share/icons/hicolor/ 2>/dev/null || true
	update-desktop-database $(HOME)/.local/share/applications/ 2>/dev/null || true
	@echo "Installed to application menu.  Launch 'Cloudflare WARP VPN' from your app launcher."

$(ICON_DST):
	mkdir -p $(dir $(ICON_DST))
	if [ -f $(ICON_SRC) ]; then \
		cp $(ICON_SRC) $(ICON_DST); \
	else \
		python3 tools/gen_icon.py $(ICON_DST); \
	fi

uninstall:
	rm -f $(HOME)/.local/share/applications/$(DESKTOP)
	rm -f $(ICON_DST)
	gtk-update-icon-cache $(HOME)/.local/share/icons/hicolor/ 2>/dev/null || true
	update-desktop-database $(HOME)/.local/share/applications/ 2>/dev/null || true
	@echo "Removed from application menu."

lint:
	python3 -c "import ast; ast.parse(open('$(APP)').read())"
	pyflakes $(APP) 2>/dev/null || pip install pyflakes && pyflakes $(APP)
	@echo "Lint passed."

security:
	python3 -m pip install --user --quiet bandit
	bandit -r warp_gui.py tools/ -x tests -ll
	command -v shellcheck >/dev/null 2>&1 || (echo "shellcheck is required. Install it with: sudo apt-get install shellcheck" && exit 1)
	shellcheck build.sh launcher.sh install.sh tools/build_deb.sh
	@echo "Security checks passed."

clean:
	rm -rf dist build AppDir *.spec
	rm -f $(APPIMAGE)
	@echo "Cleaned."
