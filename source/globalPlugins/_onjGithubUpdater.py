# Shared GitHub release updater for Onj NVDA add-ons.

import json
import os
import ssl
import time
import urllib.request

import addonHandler
import config
import globalVars
import gui
import wx
from core import callLater
from logHandler import log

try:
	from gui.addonGui import promptUserForRestart
except ImportError:
	promptUserForRestart = None

try:
	from systemUtils import ExecAndPump
except ImportError:
	from gui import ExecAndPump


CHECK_INTERVAL_MS = 86400 * 1000
RETRY_INTERVAL_MS = 600 * 1000
DOWNLOAD_BLOCK_SIZE = 8192


def _parseVersion(version):
	try:
		return tuple(int(part) for part in str(version).strip().lstrip("vV").replace("-dev", "").split("."))
	except Exception:
		return (0,)


def _isNewerVersion(newVersion, currentVersion):
	return _parseVersion(newVersion) > _parseVersion(currentVersion)


def _downloadFile(url, dest, update=None):
	req = urllib.request.Request(url, headers={"User-Agent": "Onj NVDA addon updater"})
	with urllib.request.urlopen(req, timeout=120) as remote:
		size = int(remote.headers.get("content-length") or 0)
		read = 0
		with open(dest, "wb") as local:
			while True:
				block = remote.read(DOWNLOAD_BLOCK_SIZE)
				if not block:
					break
				local.write(block)
				read += len(block)
				if update and size and update(int(read / size * 100)):
					return False
	return True


def _downloadWithProgress(url, dest, title, message):
	gui.mainFrame.prePopup()
	progressDialog = wx.ProgressDialog(
		title,
		message,
		style=wx.PD_CAN_ABORT | wx.PD_ELAPSED_TIME | wx.PD_REMAINING_TIME | wx.PD_AUTO_HIDE,
		parent=gui.mainFrame,
	)
	progressDialog.CentreOnScreen()
	progressDialog.Raise()

	def update(value):
		return not progressDialog.Update(value)[0]

	try:
		ExecAndPump(_downloadFile, url, dest, update)
		return True
	except Exception:
		log.error("Error downloading add-on update from %s", url, exc_info=True)
		gui.messageBox(
			_("Unable to download the update. Check your internet connection and try again later."),
			_("Error downloading update"),
			wx.OK | wx.ICON_ERROR,
			gui.mainFrame,
		)
		return False
	finally:
		progressDialog.Destroy()
		gui.mainFrame.postPopup()


def _installAddon(addonPath):
	from gui.message import DisplayableError

	try:
		bundle = addonHandler.AddonBundle(addonPath)
		prevAddon = None
		for addon in addonHandler.getAvailableAddons():
			if addon.name == bundle.manifest.get("name"):
				prevAddon = addon
				break
		result = ExecAndPump(addonHandler.installAddonBundle, bundle)
		addonObj = result.funcRes
		if getattr(bundle, "_installExceptions", None):
			for exc in bundle._installExceptions:
				log.error(exc, exc_info=True)
			raise DisplayableError(_("Failed to install add-on from %s") % addonPath)
		if prevAddon:
			prevAddon.requestRemove()
		if addonObj:
			addonObj._cleanupAddonImports()
		return True
	except Exception:
		log.error("Error installing add-on update from %s", addonPath, exc_info=True)
		gui.messageBox(
			_("Failed to install the update."),
			_("Update failed"),
			wx.OK | wx.ICON_ERROR,
			gui.mainFrame,
		)
		return False


class GitHubReleaseUpdater:
	def __init__(self, addonName, addonLabel, owner, repo):
		self.addonName = addonName
		self.addonLabel = addonLabel
		self.owner = owner
		self.repo = repo
		self.apiUrl = "https://api.github.com/repos/%s/%s/releases/latest" % (owner, repo)
		self.updatesDir = os.path.join(globalVars.appArgs.configPath, "updates")
		self.stateFile = os.path.join(globalVars.appArgs.configPath, "%sGithubUpdate.json" % addonName)
		self.timer = None
		self.isError = False
		self.state = self._loadState()

	def _loadState(self):
		try:
			with open(self.stateFile, "r", encoding="utf-8") as f:
				return json.load(f)
		except Exception:
			return {"lastCheck": 0, "pendingFile": ""}

	def _saveState(self):
		try:
			with open(self.stateFile, "w", encoding="utf-8") as f:
				json.dump(self.state, f)
		except Exception:
			log.debugWarning("Could not save add-on updater state for %s", self.addonName, exc_info=True)

	def start(self):
		if getattr(config, "isAppX", False):
			return
		self._scheduleNext()

	def stop(self):
		try:
			if self.timer and self.timer.IsRunning():
				self.timer.Stop()
		except Exception:
			pass
		self.timer = None

	def checkNow(self, fromGui=True):
		self._checkUpdate(fromGui=fromGui)

	def _scheduleNext(self):
		self.stop()
		if self.isError:
			nextTime = RETRY_INTERVAL_MS
		else:
			nextTime = int(CHECK_INTERVAL_MS - (time.time() * 1000 - self.state.get("lastCheck", 0)))
		if nextTime <= 0:
			nextTime = 10000
		self.timer = callLater(nextTime, self._autoCheckUpdate)

	def _autoCheckUpdate(self):
		wx.CallAfter(self._checkUpdate, False)

	def _currentAddon(self):
		for addon in addonHandler.getAvailableAddons():
			if addon.name == self.addonName:
				return addon
		return None

	def _getUpdateInfo(self):
		req = urllib.request.Request(self.apiUrl, headers={"User-Agent": "Onj NVDA addon updater"})
		try:
			with urllib.request.urlopen(req, timeout=20) as response:
				data = json.loads(response.read().decode("utf-8"))
		except IOError as e:
			if getattr(e, "reason", None) and isinstance(e.reason, ssl.SSLCertVerificationError):
				raise
			raise

		asset = None
		for item in data.get("assets", []):
			name = str(item.get("name", ""))
			if name.lower().endswith(".nvda-addon"):
				asset = item
				break
		if not asset:
			raise RuntimeError("No .nvda-addon asset found on latest GitHub release for %s" % self.repo)
		return {
			"version": str(data.get("tag_name", "")).lstrip("vV"),
			"name": asset["name"],
			"downloadUrl": asset["browser_download_url"],
			"body": data.get("body") or _("No release notes available."),
		}

	def _checkUpdate(self, fromGui=False):
		if getattr(config, "isAppX", False):
			return
		pendingFile = self.state.get("pendingFile", "")
		if pendingFile and os.path.exists(pendingFile):
			return self._installDownloaded(pendingFile)

		current = self._currentAddon()
		if not current:
			log.debugWarning("Could not find current add-on %s for updater", self.addonName)
			return

		try:
			info = self._getUpdateInfo()
			self.isError = False
		except Exception:
			self.isError = True
			log.debugWarning("Could not check GitHub update for %s", self.addonName, exc_info=True)
			if fromGui:
				gui.messageBox(
					_("Unable to check for updates right now."),
					_("Update check failed"),
					wx.OK | wx.ICON_ERROR,
					gui.mainFrame,
				)
			return self._scheduleNext()

		self.state["lastCheck"] = time.time() * 1000
		self._saveState()

		if not _isNewerVersion(info["version"], current.version):
			if fromGui:
				gui.messageBox(
					_("There are no updates available for %s.") % self.addonLabel,
					_("No updates available"),
					wx.OK | wx.ICON_INFORMATION,
					gui.mainFrame,
				)
			return self._scheduleNext()

		message = _(
			"A new version of {addon} is available: {version}.\n\n"
			"Do you want to download and install it now?\n\n"
			"{notes}"
		).format(addon=self.addonLabel, version=info["version"], notes=info.get("body", ""))
		res = gui.messageBox(
			message,
			_("Update available"),
			wx.YES | wx.NO | wx.ICON_INFORMATION,
			gui.mainFrame,
		)
		if res == wx.YES:
			self._downloadAndInstall(info)
		self._scheduleNext()

	def _downloadAndInstall(self, info):
		try:
			os.makedirs(self.updatesDir, exist_ok=True)
		except Exception:
			log.error("Unable to create add-on updates directory %s", self.updatesDir, exc_info=True)
			return
		dest = os.path.join(self.updatesDir, info["name"])
		if not _downloadWithProgress(
			info["downloadUrl"],
			dest,
			_("Downloading %s update") % self.addonLabel,
			_("Downloading update"),
		):
			return
		self.state["pendingFile"] = dest
		self._saveState()
		self._installDownloaded(dest)

	def _installDownloaded(self, dest):
		gui.mainFrame.prePopup()
		try:
			result = _installAddon(dest)
		finally:
			gui.mainFrame.postPopup()
		if result:
			self.state["pendingFile"] = ""
			self._saveState()
			try:
				os.remove(dest)
			except Exception:
				pass
			if promptUserForRestart:
				promptUserForRestart()
