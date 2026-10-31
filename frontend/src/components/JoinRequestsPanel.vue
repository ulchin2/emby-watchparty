<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api } from '@/api/client'
import { usePartyStore, type JoinRequestInfo } from '@/stores/party'
import { avatarUrl as fallbackAvatarUrl } from '@/utils/avatar'

const emit = defineEmits<{ (e: 'close'): void }>()

const party = usePartyStore()
const panelEl = ref<HTMLElement | null>(null)

// Ticking clock for the "waiting 0:42" labels. It also refreshes the
// expiry hint without requiring another server event.
const now = ref(Date.now())
let tick: ReturnType<typeof setInterval> | null = null

// Decline is a two-step action in the list: the first click arms the
// button, the second confirms. This makes an accidental click on the
// destructive action recoverable, which is the other half of the
// "clicked the wrong thing" complaint.
const confirmDeclineId = ref<string | null>(null)
let confirmTimer: ReturnType<typeof setTimeout> | null = null

const requests = computed(() => party.joinRequests)

function formatDuration(totalSeconds: number): string {
  const safe = Math.max(0, Math.floor(totalSeconds))
  const mm = Math.floor(safe / 60).toString().padStart(2, '0')
  const ss = (safe % 60).toString().padStart(2, '0')
  return `${mm}:${ss}`
}

function waitingLabel(request: JoinRequestInfo): string {
  return formatDuration((now.value - request.requestedAt) / 1000)
}

function avatarSrc(request: JoinRequestInfo): string {
  return request.avatar_uuid
    ? api.avatarSrc(request.avatar_uuid)
    : fallbackAvatarUrl(request.username)
}

function approve(request: JoinRequestInfo) {
  party.decideJoinRequest(request.id, 'approve')
}

function decline(request: JoinRequestInfo) {
  if (confirmDeclineId.value !== request.id) {
    confirmDeclineId.value = request.id
    if (confirmTimer) clearTimeout(confirmTimer)
    confirmTimer = setTimeout(() => {
      confirmDeclineId.value = null
      confirmTimer = null
    }, 3500)
    return
  }
  if (confirmTimer) {
    clearTimeout(confirmTimer)
    confirmTimer = null
  }
  confirmDeclineId.value = null
  party.decideJoinRequest(request.id, 'deny')
}

function onKeyDown(event: KeyboardEvent) {
  if (event.key === 'Escape') emit('close')
}

function onPointerDown(event: PointerEvent) {
  const target = event.target as Element | null
  if (panelEl.value && target && panelEl.value.contains(target)) return
  // The header chip toggles this panel. Without this guard the capture
  // listener closes it on pointerdown and the chip's click reopens it,
  // making the toggle look broken.
  if (target?.closest?.('.join-requests-chip, .join-toast')) return
  emit('close')
}

onMounted(() => {
  tick = setInterval(() => {
    now.value = Date.now()
  }, 1000)
  document.addEventListener('keydown', onKeyDown)
  document.addEventListener('pointerdown', onPointerDown, true)
})

onUnmounted(() => {
  if (tick) clearInterval(tick)
  if (confirmTimer) clearTimeout(confirmTimer)
  document.removeEventListener('keydown', onKeyDown)
  document.removeEventListener('pointerdown', onPointerDown, true)
})
</script>

<template>
  <div
    ref="panelEl"
    class="join-panel"
    role="dialog"
    aria-label="Join requests"
    @pointerdown.stop
  >
    <div class="panel-header">
      <div class="panel-heading">
        <h3>Join requests</h3>
        <span v-if="requests.length" class="panel-count">{{ requests.length }}</span>
      </div>
      <button class="panel-close" aria-label="Close join requests" @click="emit('close')">
        Close
      </button>
    </div>

    <div v-if="requests.length" class="request-list">
      <div v-for="request in requests" :key="request.id" class="request-row">
        <img :src="avatarSrc(request)" class="request-avatar" alt="" />
        <div class="request-info">
          <div class="request-name">{{ request.username }}</div>
          <div class="request-meta">waiting {{ waitingLabel(request) }}</div>
        </div>
        <div class="request-actions">
          <button class="request-admit" @click="approve(request)">Admit</button>
          <button
            class="request-decline"
            :class="{ confirm: confirmDeclineId === request.id }"
            @click="decline(request)"
          >
            {{ confirmDeclineId === request.id ? 'Confirm' : 'Decline' }}
          </button>
        </div>
      </div>
    </div>

    <div v-else class="panel-empty">
      Nobody is waiting right now.
    </div>

    <p class="panel-note">
      Requests stay here until you admit or decline them. Dismissing the
      notification, clicking outside it, or closing this panel does not
      reject anyone or start a cooldown.
    </p>
  </div>
</template>

<style scoped>
.join-panel {
  position: fixed;
  top: 92px;
  right: 18px;
  z-index: 8550;
  width: min(380px, calc(100vw - 36px));
  max-height: min(65vh, 560px);
  display: flex;
  flex-direction: column;
  background: var(--bg-secondary);
  border: 1px solid var(--border-hover);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  overflow: hidden;
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 16px 10px;
  border-bottom: 1px solid var(--border-subtle);
}

.panel-heading {
  display: flex;
  align-items: center;
  gap: 8px;
}

.panel-heading h3 {
  margin: 0;
  font-size: 0.95rem;
  font-weight: 600;
  color: var(--text-primary);
}

.panel-count {
  min-width: 20px;
  padding: 1px 7px;
  border-radius: var(--radius-full);
  background: var(--accent-primary-dim);
  color: var(--accent-primary);
  font-size: 0.72rem;
  font-weight: 700;
  text-align: center;
}

.panel-close {
  background: transparent;
  border: 0;
  color: var(--text-muted);
  font-family: var(--font-sans);
  font-size: 0.78rem;
  cursor: pointer;
}

.panel-close:hover {
  color: var(--text-primary);
}

.request-list {
  overflow-y: auto;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.request-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px;
  border-radius: 10px;
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
}

.request-avatar {
  width: 34px;
  height: 34px;
  border-radius: 50%;
  object-fit: cover;
  flex-shrink: 0;
  background: var(--bg-surface-hover);
}

.request-info {
  flex: 1;
  min-width: 0;
}

.request-name {
  font-size: 0.88rem;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.request-meta {
  margin-top: 1px;
  font-size: 0.72rem;
  color: var(--text-muted);
}

.request-actions {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}

.request-actions button {
  padding: 5px 11px;
  border-radius: var(--radius-full);
  font-family: var(--font-sans);
  font-size: 0.74rem;
  font-weight: 600;
  cursor: pointer;
  transition: background var(--transition-fast), border-color var(--transition-fast), color var(--transition-fast);
}

.request-admit {
  background: var(--accent-green-dim);
  color: var(--accent-green);
  border: 1px solid rgba(52, 211, 153, 0.35);
}

.request-admit:hover {
  background: rgba(52, 211, 153, 0.2);
}

.request-decline {
  background: transparent;
  color: var(--text-muted);
  border: 1px solid var(--border-subtle);
}

.request-decline:hover {
  color: #fb7185;
  border-color: rgba(244, 63, 94, 0.35);
}

.request-decline.confirm {
  background: rgba(244, 63, 94, 0.14);
  color: #fb7185;
  border-color: rgba(244, 63, 94, 0.45);
}

.panel-empty {
  padding: 24px 16px;
  text-align: center;
  font-size: 0.85rem;
  color: var(--text-muted);
}

.panel-note {
  margin: 0;
  padding: 10px 16px 12px;
  border-top: 1px solid var(--border-subtle);
  font-size: 0.73rem;
  line-height: 1.45;
  color: var(--text-muted);
}

@media (max-width: 600px) {
  .join-panel {
    top: 84px;
    right: 10px;
    left: 10px;
    width: auto;
    max-height: 70vh;
  }
}
</style>
