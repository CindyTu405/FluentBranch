// Enter 鍵行為的共用工具（手機端 Enter 換行、PC 端 Enter 送出）

// 手機斷點：與程式碼其他處一致，以 768px 區分
export const MOBILE_BREAKPOINT = 768;

export function isMobileViewport() {
  return window.innerWidth < MOBILE_BREAKPOINT;
}

// 依螢幕寬度決定 Enter 鍵行為：
// - 手機（window.innerWidth < 768）：不攔截 Enter，讓瀏覽器預設插入換行
// - PC（window.innerWidth >= 768）：Enter 呼叫 onSubmit；Shift + Enter 換行（不攔截）
// onSubmit：PC 端按下 Enter 時的動作（主輸入框送出；分支編輯框分支並發送）
export function handleEnterKey(e, onSubmit) {
  if (e.key !== 'Enter' || e.shiftKey) return;
  if (isMobileViewport()) return;
  e.preventDefault();
  onSubmit();
}
