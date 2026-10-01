import Cocoa
import WebKit
import CoreGraphics

// Окно наставника на рабочем столе.
//
// Геометрия — ЖЁСТКО правая половина встроенного экрана MacBook на всю
// доступную высоту (visibleFrame: без менюбара и Дока). Окно нельзя
// перетащить или растянуть; любые сдвиги извне возвращаются на место:
//   • смена разрешения/масштаба («Больше места»), подключение/отключение
//     монитора, закрытая крышка → didChangeScreenParameters;
//   • Док/менюбар скрылись или появились (меняется visibleFrame без
//     уведомления), Stage Manager, менеджеры окон (Rectangle и т.п.),
//     Mission Control → сторож раз в 3 с сверяет frame с эталоном;
//   • сон/пробуждение, смена Space → didWake / activeSpaceDidChange;
//   • падение процесса WebKit с контентом → перезагрузка страницы;
//   • падение самого окна → launchd KeepAlive перезапускает;
//   • второй экземпляр (ручной запуск поверх launchd) → flock, выходит сразу.
//
// Данные: окно следит за data/ и при изменении tasks.json, reminders.json,
// расписания или истории наставника вызывает
// `scripts/mentor_widget_action.py render` — списки в окне всегда свежие,
// без участия Claude. Галочки: JS → postMessage → проверка → тот же скрипт
// `complete` (общая логика с ботом, см. task_actions.py).

// Корень проекта — от расположения бинарника: <проект>/scripts/native_widget/MentorWidget.
let projectDir: String = {
    let exe = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
    return exe.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().path
}()
let dataDir = projectDir + "/data"
let feedPath = dataDir + "/mentor_feed.html"
let pythonPath = projectDir + "/venv/bin/python3"
let actionScript = projectDir + "/scripts/mentor_widget_action.py"

// Файлы, от которых зависит содержимое окна.
let watchedInputs = [
    "tasks.json", "reminders.json", "schedule_cache.json", "schedule_overrides.json",
    "schedule_vk_digest.json", "yac_schedule.json", "mentor_history.json", "mentor_quote_state.json",
]

class WidgetWindow: NSWindow {
    override var canBecomeKey: Bool { true }     // нужен клик по галочкам
    override var canBecomeMain: Bool { false }
}

class AppDelegate: NSObject, NSApplicationDelegate, WKScriptMessageHandler, WKNavigationDelegate {
    var window: WidgetWindow!
    var webView: WKWebView!
    var dirWatcher: DispatchSourceFileSystemObject?
    var mtimes: [String: Date] = [:]
    var feedMtime: Date?
    var renderPending: DispatchWorkItem?
    var renderRunning = false
    var renderAgain = false
    var lastRenderDay = ""
    var reloadRetry: DispatchWorkItem?
    var actionInFlight = false
    var refreshInFlight = false
    var lastRefreshResult: (Bool, String)? = nil
    var actionTimes: [Date] = []
    let nonce = UUID().uuidString

    // MARK: запуск

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)

        window = WidgetWindow(
            contentRect: targetFrame(),
            styleMask: [.borderless],
            backing: .buffered,
            defer: false
        )
        window.title = "Наставник"
        window.isMovable = false
        window.isMovableByWindowBackground = false
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = true
        window.isReleasedWhenClosed = false
        // Выше значков рабочего стола, ниже обычных окон — «виджет на столе».
        window.level = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.desktopIconWindow)) + 1)
        window.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle, .fullScreenAuxiliary]

        let controller = WKUserContentController()
        controller.add(self, name: "mentor")
        // Одноразовый nonce страницы: сообщения без него игнорируются.
        controller.addUserScript(WKUserScript(
            source: "window.__mentorNonce = '\(nonce)';",
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        ))
        let config = WKWebViewConfiguration()
        config.userContentController = controller

        webView = WKWebView(frame: window.contentView!.bounds, configuration: config)
        webView.autoresizingMask = [.width, .height]
        webView.setValue(false, forKey: "drawsBackground")
        webView.navigationDelegate = self
        window.contentView!.addSubview(webView)

        loadFeed()
        window.orderFront(nil)

        let nc = NotificationCenter.default
        nc.addObserver(self, selector: #selector(enforceFrame),
                       name: NSApplication.didChangeScreenParametersNotification, object: nil)
        nc.addObserver(self, selector: #selector(enforceFrame),
                       name: NSWindow.didMoveNotification, object: window)
        nc.addObserver(self, selector: #selector(enforceFrame),
                       name: NSWindow.didResizeNotification, object: window)
        let wnc = NSWorkspace.shared.notificationCenter
        wnc.addObserver(self, selector: #selector(didWake),
                        name: NSWorkspace.didWakeNotification, object: nil)
        wnc.addObserver(self, selector: #selector(enforceFrame),
                        name: NSWorkspace.activeSpaceDidChangeNotification, object: nil)

        // Сторож геометрии + смена суток + страховка, если события ФС пропали.
        Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            self?.enforceFrame()
        }
        Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            self?.checkInputs(forceDayCheck: true)
        }

        snapshotMtimes()
        lastRenderDay = today()
        watchDataDir()
        scheduleRender()   // сразу пересобрать — данные могли измениться, пока окна не было
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    // MARK: геометрия

    func builtInScreen() -> NSScreen? {
        NSScreen.screens.first { screen in
            guard let num = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber else { return false }
            return CGDisplayIsBuiltin(CGDirectDisplayID(num.uint32Value)) != 0
        }
    }

    /// Правая половина встроенного экрана (при закрытой крышке — основного).
    func targetFrame() -> NSRect {
        let screen = builtInScreen() ?? NSScreen.main ?? NSScreen.screens.first
        guard let vf = screen?.visibleFrame else { return NSRect(x: 0, y: 0, width: 600, height: 800) }
        let half = floor(vf.width / 2)
        return NSRect(x: vf.maxX - half, y: vf.minY, width: half, height: vf.height)
    }

    @objc func enforceFrame() {
        guard let window = window else { return }
        let target = targetFrame()
        if !NSEqualRects(window.frame.integral, target.integral) {
            window.setFrame(target, display: true, animate: false)
        }
        if !window.isVisible { window.orderFront(nil) }
    }

    @objc func didWake() {
        enforceFrame()
        checkInputs(forceDayCheck: true)
        // После сна WebContent-процесс иногда остаётся с пустой страницей.
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in self?.requestReload() }
    }

    // MARK: страница

    func loadFeed() {
        let url = URL(fileURLWithPath: feedPath)
        webView.loadFileURL(url, allowingReadAccessTo: url.deletingLastPathComponent())
    }

    /// Перезагрузка, но не посреди подтверждения галочки/отсчёта отмены.
    func requestReload() {
        reloadRetry?.cancel()
        webView.evaluateJavaScript("window.mentorCanReload ? window.mentorCanReload() : true") { [weak self] result, _ in
            guard let self = self else { return }
            if let can = result as? Bool, can == false {
                let work = DispatchWorkItem { [weak self] in self?.requestReload() }
                self.reloadRetry = work
                DispatchQueue.main.asyncAfter(deadline: .now() + 2, execute: work)
                return
            }
            self.loadFeed()
        }
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        NSLog("MentorWidget: WebContent упал — перезагружаем")
        loadFeed()
    }

    // Ссылки (вебинары) — во внешнем браузере; внутри окна грузится только наш файл.
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if url.scheme == "http" || url.scheme == "https" {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        if url.isFileURL && url.path == feedPath {
            decisionHandler(.allow)
            return
        }
        decisionHandler(navigationAction.navigationType == .other && url.absoluteString == "about:blank" ? .allow : .cancel)
    }

    // MARK: слежение за данными

    func today() -> String {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: Date())
    }

    func mtime(_ path: String) -> Date? {
        (try? FileManager.default.attributesOfItem(atPath: path))?[.modificationDate] as? Date
    }

    func snapshotMtimes() {
        for name in watchedInputs { mtimes[name] = mtime(dataDir + "/" + name) }
        feedMtime = mtime(feedPath)
    }

    /// data/ — один вотчер на каталог: все писатели проекта пишут атомарно
    /// (temp + os.replace), это всегда событие каталога, а inode самих
    /// файлов при этом меняется (вотчер на файл «слеп» после первой записи).
    func watchDataDir() {
        dirWatcher?.cancel()
        let fd = open(dataDir, O_EVTONLY)
        guard fd >= 0 else {
            DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in self?.watchDataDir() }
            return
        }
        let source = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd, eventMask: [.write, .rename, .delete, .link], queue: .main)
        source.setEventHandler { [weak self] in self?.checkInputs(forceDayCheck: false) }
        source.setCancelHandler { close(fd) }
        source.resume()
        dirWatcher = source
    }

    func checkInputs(forceDayCheck: Bool) {
        var inputsChanged = false
        for name in watchedInputs {
            let m = mtime(dataDir + "/" + name)
            if m != mtimes[name] { mtimes[name] = m; inputsChanged = true }
        }
        if forceDayCheck && today() != lastRenderDay { inputsChanged = true }
        if inputsChanged { scheduleRender() }

        let fm = mtime(feedPath)
        if fm != feedMtime {
            feedMtime = fm
            requestReload()
        }
    }

    func scheduleRender() {
        renderPending?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.runRender() }
        renderPending = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0, execute: work)   // дебаунс пачки записей
    }

    func runRender() {
        if renderRunning { renderAgain = true; return }
        renderRunning = true
        lastRenderDay = today()
        runPython(["render"], timeout: 60) { [weak self] _ in
            guard let self = self else { return }
            self.renderRunning = false
            if self.renderAgain { self.renderAgain = false; self.scheduleRender() }
            self.checkInputs(forceDayCheck: false)
        }
    }

    // MARK: Python

    func runPython(_ args: [String], timeout: TimeInterval, completion: @escaping ([String: Any]?) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: pythonPath)
            p.arguments = [actionScript] + args
            p.currentDirectoryURL = URL(fileURLWithPath: projectDir)
            let out = Pipe()
            p.standardOutput = out
            p.standardError = FileHandle.nullDevice
            var result: [String: Any]? = nil
            do {
                try p.run()
                let killer = DispatchWorkItem { if p.isRunning { p.terminate() } }
                DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: killer)
                // Скрипт закрывает stdout сразу после ответа и доделывает
                // медленный хвост (Telegram, «Напоминания») уже без нас.
                let data = out.fileHandleForReading.readDataToEndOfFile()
                DispatchQueue.global().async { p.waitUntilExit(); killer.cancel() }
                if let line = String(data: data, encoding: .utf8)?
                    .split(separator: "\n").last,
                   let json = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any] {
                    result = json
                }
            } catch {
                NSLog("MentorWidget: не удалось запустить python: \(error)")
            }
            DispatchQueue.main.async { completion(result) }
        }
    }

    // MARK: галочки

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        // Только наша страница из файла — никакой другой контент сюда писать не может.
        guard message.frameInfo.isMainFrame,
              message.frameInfo.request.url?.path == feedPath,
              let body = message.body as? [String: Any],
              body["nonce"] as? String == nonce
        else { return }
        if body["action"] as? String == "refresh" {
            startRefresh()
            return
        }
        guard body["action"] as? String == "complete",
              let id = body["id"] as? String,
              let title = body["title"] as? String,
              let req = body["req"] as? String
        else { return }

        let idOK = id.range(of: "^[A-Za-z0-9_\\-]{1,64}$", options: .regularExpression) != nil
        let reqOK = req.range(of: "^[0-9]{1,9}$", options: .regularExpression) != nil
        guard reqOK else { return }
        guard idOK, title.count <= 500 else { reply(req, ok: false, message: "некорректные данные"); return }

        // Ограничение частоты: не больше одного действия одновременно и
        // 8 в минуту — даже если в странице что-то пойдёт не так.
        let now = Date()
        actionTimes = actionTimes.filter { now.timeIntervalSince($0) < 60 }
        if actionInFlight || actionTimes.count >= 8 {
            reply(req, ok: false, message: "слишком часто, подожди немного")
            return
        }
        actionInFlight = true
        actionTimes.append(now)

        runPython(["complete", "--id", id, "--title", title], timeout: 60) { [weak self] result in
            guard let self = self else { return }
            self.actionInFlight = false
            let ok = result?["ok"] as? Bool ?? false
            let msg = result?["error"] as? String ?? (result == nil ? "скрипт не ответил" : "")
            self.reply(req, ok: ok, message: msg)
        }
    }

    // Кнопка «Обновить»: синхронизация задач/расписания/ссылок + выводы Claude.
    // Длится до пары минут; одновременно — только одно обновление.
    func startRefresh() {
        if refreshInFlight { return }
        refreshInFlight = true
        runPython(["refresh"], timeout: 320) { [weak self] result in
            guard let self = self else { return }
            self.refreshInFlight = false
            let ok = result?["ok"] as? Bool ?? false
            let msg = (result?["message"] as? String) ?? (result?["error"] as? String) ?? "обновление не ответило"
            self.lastRefreshResult = (ok, msg)
            self.deliverRefreshResult()
        }
    }

    /// Страница могла перезагрузиться по ходу обновления — доставляем
    /// результат повторно после загрузки, пока не дошло.
    func deliverRefreshResult() {
        guard let (ok, msg) = lastRefreshResult,
              let data = try? JSONSerialization.data(withJSONObject: [ok, msg]),
              let json = String(data: data, encoding: .utf8) else { return }
        webView.evaluateJavaScript("window.mentorRefreshResult ? (window.mentorRefreshResult.apply(null, \(json)), true) : false") { [weak self] res, _ in
            if (res as? Bool) == true { self?.lastRefreshResult = nil }
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if lastRefreshResult != nil {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in self?.deliverRefreshResult() }
        }
    }

    func reply(_ req: String, ok: Bool, message: String) {
        let payload: [Any] = [req, ok, message]
        guard let data = try? JSONSerialization.data(withJSONObject: payload),
              let json = String(data: data, encoding: .utf8) else { return }
        webView.evaluateJavaScript("window.mentorActionResult && window.mentorActionResult.apply(null, \(json))")
    }
}

// MARK: один экземпляр

let lockFd = open(dataDir + "/mentor_widget.pid.lock", O_CREAT | O_RDWR, 0o644)
if lockFd < 0 || flock(lockFd, LOCK_EX | LOCK_NB) != 0 {
    NSLog("MentorWidget: уже запущен — выходим")
    exit(0)
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
