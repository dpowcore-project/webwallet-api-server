(function() {
    'use strict';
    // --- Private data ---
    const networkConfigs = {
        'DPC': { name: 'Main Network (DPC)', api: `${window.location.protocol}//${window.location.host}` }
    };
    const apiMethods = [
        { name: 'GET /info', method: 'GET', id: 'info', desc: 'Current block height. Returns <code>blocks</code>', base: '/info', params: [] },
        { name: 'GET /balance/<address>', method: 'GET', id: 'balance', desc: 'Confirmed + unconfirmed balance in satoshis. Address: bech32 (dpc1q...) or base58check.', base: '/balance/', params: [{ title: 'address', key: true, placeholder: 'dpc1q... or base58 address' }] },
        { name: 'GET /unspent/<address>', method: 'GET', id: 'unspent', desc: 'UTXO list. Each item includes <code>txid</code>, <code>index</code>, <code>value</code>, <code>height</code>, <code>script</code>, <code>coinbase</code> (bool — true if mined as block reward). Optional <code>?amount=</code> to filter by minimum value (satoshis), <code>?confirmed=true</code> to skip mempool UTXOs.', base: '/unspent/', params: [{ title: 'address', key: true, placeholder: 'dpc1q... or base58 address' }, { title: 'amount', key: false, placeholder: '0  (min satoshis, optional)' }, { title: 'confirmed', key: false, placeholder: 'true  (optional)' }] },
        { name: 'GET /fee', method: 'GET', id: 'fee', desc: 'Fixed fee rate in satoshis.', base: '/fee', params: [] },
        { name: 'GET /tx/<txid>', method: 'GET', id: 'tx', desc: 'Verbose transaction details. Returns <code>vin</code>, <code>vout</code> (with <code>value_sat</code>), <code>txid</code>, sizes.', base: '/tx/', params: [{ title: 'txid', key: true, placeholder: '64-char transaction id' }] },
        { name: 'GET /history/<address>', method: 'GET', id: 'history', desc: 'Transaction history for address. Each item: <code>txid</code>, <code>height</code>, <code>timestamp</code> (unix), <code>direction</code> ("in"/"out"/"self"/"unknown"), <code>amount</code> (satoshis), <code>mine_in</code>, <code>mine_out</code>. Optional <code>?limit=</code> (default 10, max 50). <code>height&nbsp;==&nbsp;0</code> = mempool.', base: '/history/', params: [{ title: 'address', key: true, placeholder: 'dpc1q... or base58 address' }, { title: 'limit', key: false, placeholder: '10  (max 50, optional)' }] },
        { name: 'GET /rawtx/<txid>', method: 'GET', id: 'rawtx', desc: 'Raw transaction hex string. Used by the web wallet to provide <code>nonWitnessUtxo</code> when signing legacy P2PKH inputs. Returns the raw hex directly as <code>result</code>.', base: '/rawtx/', params: [{ title: 'txid', key: true, placeholder: '64-char transaction id' }] },
        { name: 'POST /broadcast', method: 'POST', id: 'broadcast', desc: 'Broadcast raw transaction hex. Send as form field <code>raw=&lt;hex&gt;</code>.', base: '/broadcast', params: [{ title: 'raw', key: false, placeholder: 'raw transaction hex (0100000...)' }] }
    ];
    let currentNetworkKey = 'DPC';
    // --- Cookie helpers ---
    function setCookie(name, value, days) {
        const expires = days ? `; expires=${new Date(Date.now() + days * 864e5).toGMTString()}` : '';
        document.cookie = `${encodeURIComponent(name)}=${encodeURIComponent(value)}${expires}; path=/`;
    }
    function getCookie(name) {
        const prefix = `${encodeURIComponent(name)}=`;
        const cookies = document.cookie.split(';');
        for (const cookie of cookies) {
            let c = cookie.trim();
            if (c.startsWith(prefix)) {
                return decodeURIComponent(c.substring(prefix.length));
            }
        }
        return null;
    }
    // --- Network switcher ---
    function displayNetworks() {
        const savedNet = getCookie('network') || 'DPC';
        currentNetworkKey = networkConfigs[savedNet] ? savedNet : 'DPC';
        const toggle = document.querySelector('#network-list .dropdown-toggle');
        if (toggle) toggle.textContent = networkConfigs[currentNetworkKey].name;

        const menu = document.querySelector('#network-list .dropdown-menu');
        if (!menu) return;
        menu.innerHTML = '';
        for (const [key, cfg] of Object.entries(networkConfigs)) {
            const item = document.createElement('a');
            item.href = '#';
            item.className = `dropdown-item ${key === currentNetworkKey ? 'active' : ''}`;
            item.textContent = cfg.name;
            item.addEventListener('click', (e) => {
                e.preventDefault();
                switchConfig(key);
            });
            menu.appendChild(item);
        }
    }
    function switchConfig(net) {
        setCookie('network', net, 60);
        displayNetworks();
        // Optionally re-render? Current API uses current host only, no need.
    }
    // --- UI building (safe DOM, no innerHTML with user data) ---
    function buildParamHtml(params) {
        const container = document.createElement('div');
        for (const p of params) {
            const group = document.createElement('div');
            group.className = 'input-group mt-2';
            const labelSpan = document.createElement('span');
            labelSpan.className = 'input-group-text';
            labelSpan.textContent = p.title;
            group.appendChild(labelSpan);
            const input = document.createElement('input');
            input.className = 'form-control docs-input';
            input.setAttribute('data-name', p.title);
            input.setAttribute('data-key', p.key ? 'true' : 'false');
            input.placeholder = p.placeholder || '';
            group.appendChild(input);

            container.appendChild(group);
        }
        return container;
    }
    function createMethodCard(method) {
        const card = document.createElement('div');
        card.className = 'card api-doc-block';
        card.setAttribute('data-base', `${window.location.protocol}//${window.location.host}${method.base}`);
        card.setAttribute('data-method', method.method);
        // Header
        const header = document.createElement('div');
        header.className = 'card-header';
        const badge = document.createElement('span');
        badge.className = `badge bg-${method.method === 'POST' ? 'danger' : 'success'}`;
        badge.textContent = method.method;
        header.appendChild(badge);
        const titleSpan = document.createElement('span');
        titleSpan.innerHTML = ` <b>${escapeHtml(method.name)}</b>`;
        header.appendChild(titleSpan);
        const runBtn = document.createElement('button');
        runBtn.className = 'btn btn-primary btn-sm float-end';
        runBtn.textContent = 'Run';
        runBtn.setAttribute('data-id', method.id);
        header.appendChild(runBtn);
        // Body
        const body = document.createElement('div');
        body.className = 'card-body';
        const descPara = document.createElement('p');
        descPara.innerHTML = method.desc; // safe: hardcoded
        body.appendChild(descPara);
        const methodDiv = document.createElement('div');
        methodDiv.id = method.id;
        const preElem = document.createElement('pre');
        preElem.className = 'json-display d-none';
        methodDiv.appendChild(preElem);
        const loadingDiv = document.createElement('div');
        loadingDiv.className = 'loading text-center d-none';
        const spinner = document.createElement('div');
        spinner.className = 'spinner-border';
        loadingDiv.appendChild(spinner);
        methodDiv.appendChild(loadingDiv);
        const paramsContainer = buildParamHtml(method.params);
        methodDiv.appendChild(paramsContainer);
        body.appendChild(methodDiv);
        card.appendChild(header);
        card.appendChild(body);

        runBtn.addEventListener('click', () => executeMethod(method.id));
        return card;
    }
    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }
    function renderDocs() {
        const container = document.getElementById('docs');
        if (!container) return;
        container.innerHTML = '';
        for (const method of apiMethods) {
            const card = createMethodCard(method);
            container.appendChild(card);
            if (method.params.length === 0) {
                executeMethod(method.id);
            }
        }
    }
    async function executeMethod(id) {
        const methodDiv = document.getElementById(id);
        if (!methodDiv) return;
        const card = methodDiv.closest('.api-doc-block');
        if (!card) return;
        const methodType = card.getAttribute('data-method');
        let baseUrl = card.getAttribute('data-base');
        let url = baseUrl;
        let body = null;
        const preElem = methodDiv.querySelector('.json-display');
        const loadingElem = methodDiv.querySelector('.loading');
        if (!preElem || !loadingElem) return;
        preElem.classList.add('d-none');
        preElem.textContent = '';
        loadingElem.classList.remove('d-none');
        if (methodType === 'POST') {
            const inputs = methodDiv.querySelectorAll('.docs-input');
            const params = new URLSearchParams();
            for (const inp of inputs) {
                const val = inp.value.trim();
                if (val) {
                    params.append(inp.getAttribute('data-name'), val);
                }
            }
            if ([...params.keys()].length === 0) {
                loadingElem.classList.add('d-none');
                preElem.textContent = 'Enter a value and click Run.';
                preElem.classList.remove('d-none');
                return;
            }
            body = params.toString();
        } else {
            const inputs = methodDiv.querySelectorAll('.docs-input');
            let keyValue = '';
            const queryParams = new URLSearchParams();
            for (const inp of inputs) {
                const val = inp.value.trim();
                if (!val) continue;
                const isKey = inp.getAttribute('data-key') === 'true';
                if (isKey) {
                    keyValue = val;
                } else {
                    queryParams.append(inp.getAttribute('data-name'), val);
                }
            }
            const hasKeyParam = methodDiv.querySelector('.docs-input[data-key="true"]') !== null;
            if (hasKeyParam && !keyValue) {
                loadingElem.classList.add('d-none');
                preElem.textContent = 'Enter an address and click Run.';
                preElem.classList.remove('d-none');
                return;
            }
            url += keyValue;
            const qs = queryParams.toString();
            if (qs) url += `?${qs}`;
        }
        try {
            const fetchOptions = { method: methodType };
            if (body) {
                fetchOptions.headers = { 'Content-Type': 'application/x-www-form-urlencoded' };
                fetchOptions.body = body;
            }
            const response = await fetch(url, fetchOptions);
            const data = await response.json();
            preElem.textContent = JSON.stringify(data, null, 2);
        } catch (err) {
            preElem.textContent = `Network error: ${err}`;
        } finally {
            loadingElem.classList.add('d-none');
            preElem.classList.remove('d-none');
        }
    }
    // --- Initialisation ---
    function init() {
        displayNetworks();
        renderDocs();
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
