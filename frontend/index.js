// --- Constants ---
const API_BASE_URL = "/api";

// --- State Management ---
const appState = {
    gameState: null,
    lastRollEventId: null,  // 用于检测骰子事件变化
};

// --- Smooth Scroll State ---
const scrollState = {
    animationId: null,
    isUserScrolling: false,
    lastScrollTop: 0,
    scrollTimeout: null,
    isFirstRender: true,  // 标记是否为首次渲染（重连后）
};

// --- DOM Elements ---
const DOMElements = {
    loginView: document.getElementById('login-view'),
    gameView: document.getElementById('game-view'),
    loginError: document.getElementById('login-error'),
    logoutButton: document.getElementById('logout-button'),
    fullscreenButton: document.getElementById('fullscreen-button'),
    narrativeWindow: document.getElementById('narrative-window'),
    characterStatus: document.getElementById('character-status'),
    opportunitiesSpan: document.getElementById('opportunities'),
    actionInput: document.getElementById('action-input'),
    actionButton: document.getElementById('action-button'),
    startTrialButton: document.getElementById('start-trial-button'),
    loadingSpinner: document.getElementById('loading-spinner'),
    rollOverlay: document.getElementById('roll-overlay'),
    rollPanel: document.getElementById('roll-panel'),
    rollType: document.getElementById('roll-type'),
    rollTarget: document.getElementById('roll-target'),
    rollResultDisplay: document.getElementById('roll-result-display'),
    rollOutcome: document.getElementById('roll-outcome'),
    rollValue: document.getElementById('roll-value'),
};

// --- API Client ---
const api = {
    async initGame() {
        const response = await fetch(`${API_BASE_URL}/game/init`, {
            method: 'POST',
            // No Authorization header needed, relies on HttpOnly cookie
        });
        if (response.status === 401) {
            throw new Error('Unauthorized');
        }
        if (!response.ok) throw new Error('Failed to initialize game session');
        return response.json();
    },
    async logout() {
        await fetch(`${API_BASE_URL}/logout`, { method: 'POST' });
        window.location.href = '/';
    }
};

// --- WebSocket Manager ---
const socketManager = {
    socket: null,
    connect() {
        return new Promise((resolve, reject) => {
            if (this.socket && this.socket.readyState === WebSocket.OPEN) {
                resolve();
                return;
            }
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const host = window.location.host;
            // The token is no longer in the URL; it's read from the cookie by the server.
            const wsUrl = `${protocol}//${host}${API_BASE_URL}/ws`;
            this.socket = new WebSocket(wsUrl);
            this.socket.binaryType = 'arraybuffer'; // Important for receiving binary data

            this.socket.onopen = () => { console.log("WebSocket established."); resolve(); };
            this.socket.onmessage = (event) => {
                let message;
                // Check if the data is binary (ArrayBuffer)
                if (event.data instanceof ArrayBuffer) {
                    try {
                        // Decompress the gzip data using pako.ungzip
                        const decompressed = pako.ungzip(new Uint8Array(event.data), { to: 'string' });
                        message = JSON.parse(decompressed);
                    } catch (err) {
                        console.error('Failed to decompress or parse message:', err);
                        return;
                    }
                } else {
                    // Fallback for non-binary messages
                    message = JSON.parse(event.data);
                }
                
                switch (message.type) {
                    case 'full_state':
                        appState.gameState = message.data;
                        checkAndShowRollEvent();
                        render();
                        break;
                    case 'patch':
                        // Apply JSON Patch
                        if (appState.gameState && message.patch) {
                            try {
                                const result = jsonpatch.applyPatch(appState.gameState, message.patch, true, false);
                                appState.gameState = result.newDocument;
                                checkAndShowRollEvent();
                                render();
                            } catch (err) {
                                console.error('Failed to apply patch:', err);
                            }
                        }
                        break;
                    case 'error':
                        alert(`WebSocket Error: ${message.detail}`);
                        break;
                }
            };
            this.socket.onclose = () => { console.log("Reconnecting..."); showLoading(true); setTimeout(() => this.connect(), 5000); };
            this.socket.onerror = (error) => { console.error("WebSocket error:", error); DOMElements.loginError.textContent = '无法连接。'; reject(error); };
        });
    },
    sendAction(action) {
        if (this.socket && this.socket.readyState === WebSocket.OPEN) {
            this.socket.send(JSON.stringify({ action }));
        } else {
            alert("连接已断开，请刷新。");
        }
    }
};

// --- UI & Rendering ---
function showView(viewId) {
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    document.getElementById(viewId).classList.add('active');
}

// --- Smooth Scroll Functions ---
function stopSmoothScroll() {
    if (scrollState.animationId) {
        cancelAnimationFrame(scrollState.animationId);
        scrollState.animationId = null;
    }
}

function smoothScrollToBottom(element, pixelsPerSecond = 150) {
    // 停止之前的滚动动画
    stopSmoothScroll();
    
    // 如果用户正在手动滚动，不启动自动滚动
    if (scrollState.isUserScrolling) {
        return;
    }
    
    const startScrollTop = element.scrollTop;
    const minScrollDistance = 50; // 最小滚动距离阈值
    
    function tryStartScroll(retryCount = 0) {
        const targetScrollTop = element.scrollHeight - element.clientHeight;
        const distance = targetScrollTop - startScrollTop;
        
        // 如果滚动空间太小，等待内容加载（最多等1秒，每100ms检查一次）
        if (distance < minScrollDistance && retryCount < 10) {
            setTimeout(() => tryStartScroll(retryCount + 1), 100);
            return;
        }
        
        // 如果等了1秒还是没有足够的滚动空间，放弃
        if (distance <= 0) {
            return;
        }
        
        // 再次检查用户是否开始手动滚动
        if (scrollState.isUserScrolling) {
            return;
        }
        
        const startTime = performance.now();
        const duration = (distance / pixelsPerSecond) * 1000;
        
        function animateScroll(currentTime) {
            if (scrollState.isUserScrolling) {
                scrollState.animationId = null;
                return;
            }
            
            const elapsed = currentTime - startTime;
            const progress = Math.min(elapsed / duration, 1);
            const easeProgress = 1 - (1 - progress) * (1 - progress);
            
            element.scrollTop = startScrollTop + (distance * easeProgress);
            
            if (progress < 1) {
                scrollState.animationId = requestAnimationFrame(animateScroll);
            } else {
                scrollState.animationId = null;
            }
        }
        
        scrollState.animationId = requestAnimationFrame(animateScroll);
    }
    
    tryStartScroll();
}

function setupScrollInterruptListener(element) {
    // 检测用户滚轮滚动
    element.addEventListener('wheel', () => {
        scrollState.isUserScrolling = true;
        stopSmoothScroll();
        
        // 清除之前的超时
        if (scrollState.scrollTimeout) {
            clearTimeout(scrollState.scrollTimeout);
        }
        
        // 2秒后重置用户滚动状态，允许下次自动滚动
        scrollState.scrollTimeout = setTimeout(() => {
            scrollState.isUserScrolling = false;
        }, 2000);
    }, { passive: true });
    
    // 检测触摸滚动
    element.addEventListener('touchstart', () => {
        scrollState.isUserScrolling = true;
        stopSmoothScroll();
    }, { passive: true });
    
    element.addEventListener('touchend', () => {
        if (scrollState.scrollTimeout) {
            clearTimeout(scrollState.scrollTimeout);
        }
        scrollState.scrollTimeout = setTimeout(() => {
            scrollState.isUserScrolling = false;
        }, 2000);
    }, { passive: true });
}

function showLoading(isLoading) {
    // 只在初始化时显示全屏加载（gameState 为空时）
    const showFullscreenSpinner = isLoading && !appState.gameState;
    DOMElements.loadingSpinner.style.display = showFullscreenSpinner ? 'flex' : 'none';
    
    const isProcessing = appState.gameState ? appState.gameState.is_processing : false;
    const buttonsDisabled = isLoading || isProcessing;
    // DOMElements.loginButton is removed
    DOMElements.actionInput.disabled = buttonsDisabled;
    DOMElements.actionButton.disabled = buttonsDisabled;
    DOMElements.startTrialButton.disabled = buttonsDisabled;
    
    // 在按钮上显示加载状态
    if (buttonsDisabled && appState.gameState) {
        DOMElements.actionButton.textContent = '⏳';
    } else {
        DOMElements.actionButton.textContent = '定';
    }
}

function render() {
    if (!appState.gameState) { showLoading(true); return; }
    showLoading(appState.gameState.is_processing);
    DOMElements.opportunitiesSpan.textContent = appState.gameState.opportunities_remaining;
    renderCharacterStatus();

    const historyContainer = document.createDocumentFragment();
    (appState.gameState.display_history || []).forEach(text => {
        const p = document.createElement('div');
        p.innerHTML = marked.parse(text);
        if (text.startsWith('> ')) p.classList.add('user-input-message');
        else if (text.startsWith('【')) p.classList.add('system-message');
        historyContainer.appendChild(p);
    });
    DOMElements.narrativeWindow.innerHTML = '';
    DOMElements.narrativeWindow.appendChild(historyContainer);
    
    // 首次渲染直接跳到底部，之后使用平滑滚动
    if (scrollState.isFirstRender) {
        DOMElements.narrativeWindow.scrollTop = DOMElements.narrativeWindow.scrollHeight;
        scrollState.isFirstRender = false;
    } else {
        smoothScrollToBottom(DOMElements.narrativeWindow, 150);
    }
    
    const { is_in_trial, daily_success_achieved, opportunities_remaining } = appState.gameState;
    DOMElements.actionInput.parentElement.classList.toggle('hidden', !(is_in_trial || daily_success_achieved || opportunities_remaining < 0));
    const startButton = DOMElements.startTrialButton;
    startButton.classList.toggle('hidden', is_in_trial || daily_success_achieved || opportunities_remaining < 0);

    if (daily_success_achieved) {
         startButton.textContent = "今日功德圆满";
         startButton.disabled = true;
    } else if (opportunities_remaining <= 0) {
        startButton.textContent = "机缘已尽";
        startButton.disabled = true;
    } else {
        if (opportunities_remaining === 10) {
            startButton.textContent = "开始第一次试炼";
        } else {
            startButton.textContent = "开启下一次试炼";
        }
        startButton.disabled = appState.gameState.is_processing;
    }
}

function renderValue(container, value, level = 0) {
    if (Array.isArray(value)) {
        value.forEach(item => renderValue(container, item, level + 1));
    } else if (typeof value === 'object' && value !== null) {
        const subContainer = document.createElement('div');
        subContainer.style.paddingLeft = `${level * 10}px`;
        Object.entries(value).forEach(([key, val]) => {
            const propDiv = document.createElement('div');
            propDiv.classList.add('property-item');
            
            const keySpan = document.createElement('span');
            keySpan.classList.add('property-key');
            keySpan.textContent = `${key}: `;
            propDiv.appendChild(keySpan);

            // Recursively render the value
            renderValue(propDiv, val, level + 1);
            subContainer.appendChild(propDiv);
        });
        container.appendChild(subContainer);
    } else {
        const valueSpan = document.createElement('span');
        valueSpan.classList.add('property-value');
        valueSpan.textContent = value;
        container.appendChild(valueSpan);
    }
}

function renderCharacterStatus() {
    const { current_life } = appState.gameState;
    const container = DOMElements.characterStatus;
    container.innerHTML = ''; // Clear previous content

    if (!current_life) {
        container.textContent = '静待天命...';
        return;
    }

    Object.entries(current_life).forEach(([key, value]) => {
        const details = document.createElement('details');
        const summary = document.createElement('summary');
        summary.textContent = key;
        details.appendChild(summary);

        const content = document.createElement('div');
        content.classList.add('details-content');
        
        renderValue(content, value);
        
        details.appendChild(content);
        container.appendChild(details);
    });
}

function checkAndShowRollEvent() {
    const rollEvent = appState.gameState?.roll_event;
    if (rollEvent && rollEvent.id && rollEvent.id !== appState.lastRollEventId) {
        appState.lastRollEventId = rollEvent.id;
        renderRollEvent(rollEvent);
    }
}

function renderRollEvent(rollEvent) {
    DOMElements.rollType.textContent = `判定: ${rollEvent.type}`;
    DOMElements.rollTarget.textContent = `(<= ${rollEvent.target})`;
    DOMElements.rollOutcome.textContent = rollEvent.outcome;
    DOMElements.rollOutcome.className = `outcome-${rollEvent.outcome}`;
    DOMElements.rollValue.textContent = rollEvent.result;
    DOMElements.rollResultDisplay.classList.add('hidden');
    DOMElements.rollOverlay.classList.remove('hidden');
    setTimeout(() => DOMElements.rollResultDisplay.classList.remove('hidden'), 1000);
    setTimeout(() => DOMElements.rollOverlay.classList.add('hidden'), 3000);
}

// --- Fullscreen Management ---
function toggleFullscreen() {
    document.body.classList.toggle('app-fullscreen');
    updateFullscreenButton();
}

function updateFullscreenButton() {
    const isFullscreen = document.body.classList.contains('app-fullscreen');
    if (DOMElements.fullscreenButton) {
        DOMElements.fullscreenButton.textContent = isFullscreen ? '⛶' : '⛶';
        DOMElements.fullscreenButton.title = isFullscreen ? '退出全屏' : '全屏模式';
    }
}

// --- Event Handlers ---
function handleLogout() {
    api.logout();
}

function handleAction(actionOverride = null) {
    const action = actionOverride || DOMElements.actionInput.value.trim();
    if (!action) return;

    // Special case for starting a trial to prevent getting locked out by is_processing flag
    if (action === "开始试炼") {
        // Allow starting a new trial even if the previous async task is in its finally block
    } else {
        // For all other actions, prevent sending if another action is in flight.
        if (appState.gameState && appState.gameState.is_processing) return;
    }

    DOMElements.actionInput.value = '';
    socketManager.sendAction(action);
}

// --- Initialization ---
async function initializeGame() {
    showLoading(true);
    try {
        const initialState = await api.initGame();
        appState.gameState = initialState;
        render();
        showView('game-view');
        await socketManager.connect();
        console.log("Initialization complete and WebSocket is ready.");
    } catch (error) {
        // If init fails (e.g. no valid cookie), just show the login view.
        // The api.initGame function no longer redirects, it just throws an error.
        showView('login-view');
        if (error.message !== 'Unauthorized') {
             console.error(`Session initialization failed: ${error.message}`);
        }
    } finally {
        // Ensure spinner is hidden regardless of outcome
        showLoading(false);
    }
}

function init() {
    // Always try to initialize the game on page load.
    // If the user is logged in, it will show the game view.
    // If not, the catch block in initializeGame will handle showing the login view.
    initializeGame();

    // Setup scroll interrupt listener
    setupScrollInterruptListener(DOMElements.narrativeWindow);

    // Setup event listeners regardless of initial view
    DOMElements.logoutButton.addEventListener('click', handleLogout);
    DOMElements.fullscreenButton.addEventListener('click', toggleFullscreen);
    DOMElements.actionButton.addEventListener('click', () => handleAction());
    DOMElements.actionInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') handleAction(); });
    DOMElements.startTrialButton.addEventListener('click', () => handleAction("开始试炼"));
}

// --- Start the App ---
init();