/**
 * Full Capacity — Shared frontend utilities
 */

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

async function logout() {
    try {
        await fetch('/auth/logout', { method: 'POST' });
    } catch (e) {
        // ignore
    }
    window.location.href = '/login';
}
