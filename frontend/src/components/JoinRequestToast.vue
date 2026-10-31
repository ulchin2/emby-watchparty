<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { usePartyStore, type JoinRequestInfo } from '@/stores/party'
import { avatarUrl as fallbackAvatarUrl } from '@/utils/avatar'

const props = defineProps<{ panelOpen?: boolean }>()
const emit = defineEmits<{ (e: 'review'): void }>()

const party = usePartyStore()
const auth = useAuthStore()

const current = ref<JoinRequestInfo | null>(null)
const visible = ref(false)
const toastEl = ref<HTMLElement | null>(null)
// Requests the host has already seen/acted on locally. Kept so a queue
// re-broadcast cannot resurrect a toast the host just dismissed.
const handled = new Set<string>()
let hideTimer: ReturnType<typeof setTimeout> | null = null

const otherCount = computed(() => {
  const id = current.value?.id
  return party.joinRequests.filter((request) => request.id !== id).length
})

function avatarSrc(request: JoinRequestInfo): string {
  return request.avatar_uuid
    ? api.avatarSrc(request.avatar_uuid)
    : fallbackAvatarUrl(request.username)
}

function clearHideTimer() {
  if (hideTimer) {
    clearTimeout(hideTimer)
    hideTimer = null
  }
}

function scheduleHide() {
  clearHideTimer()
  // The request itself is unaffected; this only hides the toast.
  hideTimer = setTimeout(() => {
    dismiss()
  }, 30000)
}

function showNext() {
  if (!auth.isHost) {
    visible.value = false
    current.value = null
    return
  }
  const next = party.joinRequests.find((request) => !handled.has(request.id))
  if (!next) {
    visible.value = false
    current.value = null
    return
  }
  current.value = next
  visible.value = true
  scheduleHide()
}

/** Hide the toast. The request remains in the host's Join requests list. */
function dismiss() {
  if (current.value) handled.add(current.value.id)
  visible.value = false
  current.value = null
  clearHideTimer()
  // Intentionally do NOT pop the next request as a new toast. "Later"
  // should quiet the notification; the header badge keeps the remaining
  // count visible and the list stays one click away.
}

function approve() {
  const request = current.value
  if (!request) return
  handled.add(request.id)
  party.decideJoinRequest(request.id, 'approve')
  visible.value = false
  current.value = null
  clearHideTimer()
}

function review() {
  emit('review')
  if (current.value) handled.add(current.value.id)
  visible.value = false
  current.value = null
  clearHideTimer()
}

// Drop handled markers once the server removes the request, so the set
// cannot grow forever in a long-lived party tab.
watch(
  () => party.joinRequests.map((request) => request.id),
  (ids) => {
    for (const id of Array.from(handled)) {
      if (!ids.includes(id)) handled.delete(id)
    }
    if (!auth.isHost) {
      visible.value = false
      current.value = null
      return
    }
    if (!visible.value) {
      showNext()
    } else if (current.value && !ids.includes(current.value.id)) {
      // Resolved elsewhere (another tab / host page).
      current.value = null
      visible.value = false
      showNext()
    }
  },
  { deep: true },
)

watch(
  () => props.panelOpen,
  (open) => {
    // The panel occupies the same corner; keeping the toast on top of it
    // just hides the list header.
    if (open && visible.value) dismiss()
  },
)

watch(
  () => auth.isHost,
  (isHost) => {
    if (!isHost) {
      visible.value = false
      current.value = null
      clearHideTimer()
      return
    }
    // auth.refresh() resolves after sync_state on a page load, so the
    // queue may already be populated by the time we learn we are host.
    if (!visible.value) showNext()
  },
)

function onPointerDown(event: PointerEvent) {
  if (!visible.value) return
  const target = event.target as Node | null
  if (toastEl.value && target && toastEl.value.contains(target)) return
  // Clicking anywhere outside the notification dismisses it only. It
  // must never reject the request, which is the whole point of the
  // persistent list.
  dismiss()
}

onMounted(() => {
  document.addEventListener('pointerdown', onPointerDown, true)
  showNext()
})

onUnmounted(() => {
  document.removeEventListener('pointerdown', onPointerDown, true)
  clearHideTimer()
})
</script>

<template>
  <Transition name="join-toast">
    <div
      v-if="visible && current"
      ref="toastEl"
      class="join-toast"
      role="alertdialog"
      aria-live="assertive"
      aria-label="Someone wants to join the watch party"
      @pointerdown.stop
    >
      <img :src="avatarSrc(current)" class="toast-avatar" alt="" />
      <div class="toast-body">
        <div class="toast-title">
          <strong>{{ current.username }}</strong> wants to join
        </div>
        <div class="toast-sub">
          They'll join the party.
          <span v-if="otherCount > 0" class="toast-more">
            +{{ otherCount }} more waiting
          </span>
        </div>
      </div>
      <div class="toast-actions">
        <button class="toast-admit" @click="approve">Admit</button>
        <button class="toast-later" @click="dismiss">Later</button>
        <button class="toast-review" @click="review">Review all</button>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.join-toast {
  position: fixed;
  top: 92px;
  right: 18px;
  z-index: 8600;
  width: min(390px, calc(100vw - 36px));
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
  background: var(--bg-secondary);
  border: 1px solid var(--border-hover);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg), 0 0 0 1px rgba(34, 211, 238, 0.08);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
}

.toast-avatar {
  width: 38px;
  height: 38px;
  border-radius: 50%;
  object-fit: cover;
  flex-shrink: 0;
  border: 1px solid var(--border-subtle);
  background: var(--bg-surface);
}

.toast-body {
  flex: 1;
  min-width: 0;
}

.toast-title {
  font-size: 0.92rem;
  color: var(--text-primary);
  line-height: 1.3;
}

.toast-sub {
  margin-top: 2px;
  font-size: 0.76rem;
  color: var(--text-muted);
  line-height: 1.35;
}

.toast-more {
  color: var(--accent-primary);
}

.toast-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  justify-content: flex-end;
}

.toast-actions button {
  border-radius: var(--radius-full);
  font-family: var(--font-sans);
  font-size: 0.8rem;
  font-weight: 600;
  padding: 6px 14px;
  cursor: pointer;
  transition: background var(--transition-fast), border-color var(--transition-fast), color var(--transition-fast);
}

.toast-admit {
  background: var(--accent-green-dim);
  color: var(--accent-green);
  border: 1px solid rgba(52, 211, 153, 0.35);
}

.toast-admit:hover {
  background: rgba(52, 211, 153, 0.2);
}

.toast-later {
  background: var(--bg-surface);
  color: var(--text-secondary);
  border: 1px solid var(--border-subtle);
}

.toast-later:hover {
  background: var(--bg-surface-hover);
  color: var(--text-primary);
}

.toast-review {
  background: transparent;
  color: var(--accent-primary);
  border: 1px solid transparent;
  padding-inline: 8px !important;
}

.toast-review:hover {
  text-decoration: underline;
}

.join-toast-enter-active,
.join-toast-leave-active {
  transition: opacity 0.18s ease, transform 0.18s ease;
}

.join-toast-enter-from,
.join-toast-leave-to {
  opacity: 0;
  transform: translateY(-8px);
}

@media (max-width: 600px) {
  .join-toast {
    top: 84px;
    right: 10px;
    left: 10px;
    width: auto;
  }
}
</style>
