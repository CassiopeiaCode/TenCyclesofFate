// --- Constants ---
const API_BASE_URL = "/api";

// --- State Management ---
const liveState = {
    liveGameState: null,
    playerList: [],
    watchingPlayerId: null,
};

// --- DOM Elements ---
const DOMElements = {
    playerList: document.getElementById('player-list'),
    narrativeWindow: document.getElementById('narrative-window'),
    characterStatus: document.getElementById('character-status'),
    loadingSpinner: document.getElementById('loading-spinner'),
};

// --- API Client ---
const api = {
    async getLivePlayers() {
        const response = await fetch(`${API_BASE_URL}/live/players`);
        if (!response.ok) throw new Error('Failed to fetch live players');
        return response.json();
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
            const wsUrl = `${protocol}//${host}${API_BASE_URL}/live/ws`;
            this.socket = new WebSocket(wsUrl);
            this.socket.binaryType = 'arraybuffer';

            this.socket.onopen = () => { console.log("Live WebSocket established."); resolve(); };
            this.socket.onmessage = (event) => {
                let message;
                if (event.data instanceof ArrayBuffer) {
                    try {
                        const decompressed = pako.ungzip(new Uint8Array(event.data), { to: 'string' });
                        message = JSON.parse(decompressed);
                    } catch (err) {
                        console.error('Failed to decompress or parse message:', err);
                        return;
                    }
                } else {
                    message = JSON.parse(event.data);
                }

                switch (message.type) {
                    case 'live_update':
                        liveState.liveGameState = message.data;
                        render();
                        break;
                    case 'error':
                        alert(`WebSocket Error: ${message.detail}`);
                        break;
                }
            };
            this.socket.onclose = () => { console.log("Reconnecting..."); showLoading(true); setTimeout(() => this.connect(), 5000); };
            this.socket.onerror = (error) => { console.error("WebSocket error:", error); reject(error); };
        });
    },
    watchPlayer(playerId) {
        if (this.socket && this.socket.readyState === WebSocket.OPEN) {
            this.socket.send(JSON.stringify({ action: 'watch', player_id: playerId }));
            liveState.watchingPlayerId = playerId;
            // Clear the old state immediately for better UX
            liveState.liveGameState = null; 
            render();
            showLoading(true);
        } else {
            alert("连接已断开，请刷新。");
        }
    }
};

// --- UI & Rendering ---
function showLoading(isLoading) {
    DOMElements.loadingSpinner.style.display = isLoading ? 'flex' : 'none';
}

function renderMarkdownSafe(markdownText) {
    const rawHtml = marked.parse(markdownText || "", { mangle: false, headerIds: false });
    return DOMPurify.sanitize(rawHtml, {
        USE_PROFILES: { html: true },
        FORBID_TAGS: ["script", "style", "iframe", "object", "embed", "link", "meta"],
        FORBID_ATTR: ["onerror", "onload", "onclick", "onmouseover", "onfocus", "onmouseenter", "onmouseleave", "style"],
    });
}

function render() {
    if (liveState.liveGameState) {
        showLoading(false);
    }
    renderPlayerList();
    renderNarrative();
    renderCharacterStatus();
}

function renderPlayerList() {
    const fragment = document.createDocumentFragment();
    liveState.playerList.forEach(player => {
        const playerDiv = document.createElement('div');
        playerDiv.className = 'player-list-item';
        // Compare with the real player_id for active state
        if (player.player_id === liveState.watchingPlayerId) {
            playerDiv.classList.add('active');
        }
        // Display the masked name
        playerDiv.textContent = player.display_name;
        // Use the real player_id for the watch action
        playerDiv.onclick = () => socketManager.watchPlayer(player.player_id);
        fragment.appendChild(playerDiv);
    });
    DOMElements.playerList.innerHTML = '';
    DOMElements.playerList.appendChild(fragment);
}

function renderNarrative() {
    if (!liveState.liveGameState) {
        if (!liveState.watchingPlayerId) {
            DOMElements.narrativeWindow.innerHTML = '<div class="system-message"><p>请从左侧【天机榜】选择一位道友进行观摩。</p></div>';
        } else {
            DOMElements.narrativeWindow.innerHTML = '<div class="system-message"><p>正在等待天机同步...</p></div>';
        }
        return;
    }

    const historyContainer = document.createDocumentFragment();
    (liveState.liveGameState.display_history || []).forEach(text => {
        const p = document.createElement('div');
        p.innerHTML = renderMarkdownSafe(text);
        if (text.startsWith('> ')) p.classList.add('user-input-message');
        else if (text.startsWith('【')) p.classList.add('system-message');
        historyContainer.appendChild(p);
    });
    DOMElements.narrativeWindow.innerHTML = '';
    DOMElements.narrativeWindow.appendChild(historyContainer);
    DOMElements.narrativeWindow.scrollTop = DOMElements.narrativeWindow.scrollHeight;
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
    const { current_life } = liveState.liveGameState || {};
    const container = DOMElements.characterStatus;
    container.innerHTML = '';

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

// --- Initialization ---
async function initializeLiveView() {
    showLoading(true);
    try {
        await socketManager.connect();
        const players = await api.getLivePlayers();
        liveState.playerList = players;
        render();
    } catch (error) {
        console.error("Initialization failed, redirecting to home:", error);
        window.location.href = '/';
    } finally {
        showLoading(false);
    }
}

initializeLiveView();