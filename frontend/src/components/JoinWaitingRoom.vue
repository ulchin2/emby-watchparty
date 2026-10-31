<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { usePartyStore } from '@/stores/party'

const party = usePartyStore()
const router = useRouter()

const now = ref(Date.now())
const cancelling = ref(false)
let tick: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  tick = setInterval(() => {
    now.value = Date.now()
  }, 500)
})

onUnmounted(() => {
  if (tick) clearInterval(tick)
})

function formatDuration(totalSeconds: number): string {
  const safe = Math.max(0, totalSeconds)
  const mm = Math.floor(safe / 60).toString().padStart(2, '0')
  const ss = Math.floor(safe % 60).toString().padStart(2, '0')
  return `${mm}:${ss}`
}

// Expiry countdown. null means "this deployment does not expire
// requests" (LATE_JOIN_REQUEST_TIMEOUT_SECONDS = 0), in which case the
// waiting screen shows no deadline instead of a fake one.
const timerLabel = computed(() => {
  const request = party.pendingJoinRequest
  if (!request || request.expiresAt === null) return null
  return formatDuration(Math.ceil((request.expiresAt - now.value) / 1000))
})

const waitingLabel = computed(() => {
  const request = party.pendingJoinRequest
  if (!request) return ''
  return formatDuration(Math.floor((now.value - request.requestedAt) / 1000))
})

async function cancelRequest() {
  if (cancelling.value) return
  cancelling.value = true
  try {
    // leave_party also removes the queued request server-side.
    await party.leave()
  } finally {
    router.push('/')
  }
}
</script>

<template>
  <div
    v-if="party.pendingJoinRequest"
    class="waiting-room"
    role="status"
    aria-live="polite"
  >
    <div class="waiting-content">
      <div class="spinner" aria-hidden="true"></div>
      <h2>Waiting for the host</h2>
      <p class="explain">
        Your request is in the host's <strong>Join requests</strong> list.
        The movie keeps playing for everyone else, and you'll be let in
        automatically as soon as the host admits you.
      </p>

      <div v-if="timerLabel" class="timer">
        Request expires in <strong>{{ timerLabel }}</strong>
      </div>
      <div class="progress">Waiting for {{ waitingLabel }}</div>

      <p class="hint">
        Closing a notification or panel does not decline your request.
        It stays in the host's list until they decide.
      </p>

      <button class="cancel" :disabled="cancelling" @click="cancelRequest">
        {{ cancelling ? 'Cancelling…' : 'Cancel request' }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.waiting-room {
  position: fixed;
  inset: 0;
  background: var(--bg-deep);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 9500;
}

.waiting-content {
  text-align: center;
  max-width: 460px;
  padding: 40px;
  color: var(--text-primary);
}

.spinner {
  width: 64px;
  height: 64px;
  border: 4px solid rgba(255, 255, 255, 0.1);
  border-top-color: var(--accent-primary);
  border-radius: 50%;
  animation: spin 0.9s linear infinite;
  margin: 0 auto 24px;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

h2 {
  margin: 0 0 12px;
  font-size: 1.5rem;
}

.explain {
  margin: 0 0 24px;
  color: var(--text-secondary);
  font-size: 0.95rem;
  line-height: 1.5;
}

.explain strong {
  color: var(--text-primary);
}

.timer {
  font-size: 1rem;
  color: var(--accent-primary);
  margin-bottom: 8px;
}

.timer strong {
  font-family: var(--font-mono);
  font-size: 1.2rem;
}

.progress {
  font-size: 0.9rem;
  color: var(--text-muted);
}

.hint {
  margin: 20px 0 0;
  font-size: 0.8rem;
  line-height: 1.5;
  color: var(--text-muted);
}

.cancel {
  margin-top: 20px;
  padding: 8px 18px;
  border-radius: var(--radius-full);
  border: 1px solid var(--border-subtle);
  background: var(--bg-surface);
  color: var(--text-secondary);
  font-family: var(--font-sans);
  font-size: 0.85rem;
  cursor: pointer;
  transition: background var(--transition-fast), border-color var(--transition-fast), color var(--transition-fast);
}

.cancel:hover:not(:disabled) {
  background: var(--bg-surface-hover);
  border-color: var(--border-hover);
  color: var(--text-primary);
}

.cancel:disabled {
  opacity: 0.6;
  cursor: default;
}
</style>
