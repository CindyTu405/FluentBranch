// 主對話狀態與動作：訊息、輸入框、送出／重新生成、載入對話與續讀、閱讀位置還原。分支相關只透過參數呼叫（loadBranchList、closeBranches）。
import { useState, useEffect, useRef } from 'react';
import { apiFetch } from '../api';
import { scrollToMessage } from '../scrollUtils';
import { readTextStream } from '../readTextStream';
import { QuotaError, throwIfQuotaError, MAX_MESSAGE_CHARS, TOO_LONG_MSG } from '../quotaErrors';

export function useMainChat({ model, user, fetchHistory, refreshAfterGeneration, loadBranchList, closeBranches, setSidebarOpen }) {
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState([]);
  const [isLoading, setIsLoading] = useState(false);
  // 每個對話的閱讀位置（conversation id → scrollTop）；只在記憶體中，重新整理頁面即清空。
  const [readingPositions, setReadingPositions] = useState({});
  // 捲動容器的 ref 由 App 持有，MessageList 用同一個 ref 掛在容器的 div 上
  const scrollContainerRef = useRef(null);
  // 載入對話後要做一次「還原／對齊錨點」，pendingRestore 描述要捲到哪裡；
  // restoreToken 每次載入遞增，讓 MessageList 知道要跳過一次自動捲到底部。
  const [pendingRestore, setPendingRestore] = useState(null); // { convId, scrollTop|null, anchorId|null }
  const [restoreToken, setRestoreToken] = useState(0);
  // 目前畫面上的對話 root id（null = 新對話）；串流回來時比對，避免把回覆寫進剛切換過來的對話。
  const activeRootIdRef = useRef(null);
  // 目前進行中的串流，切換對話時直接中止
  const streamAbortRef = useRef(null);
  // 送出訊息時自動捲動至底部
  const [sendScrollToken, setSendScrollToken] = useState(0);

  // 載入特定對話 (點擊側邊欄觸發)

  const loadChat = async (rootId, positionKey = rootId) => {
    // 切換對話前，先把目前對話的捲動位置記下來（key 用這串對話的 root id）
    const currentConvId = messages[0]?.id;
    const container = scrollContainerRef.current;
    if (currentConvId && container && currentConvId !== rootId) {
      const currentTop = container.scrollTop;
      setReadingPositions(prev => ({ ...prev, [currentConvId]: currentTop }));
    }

    try {
      setIsLoading(true);
      // 立刻把「目前顯示的對話」切到新的 root id，並中止正在跑的串流，
      // 讓舊對話的後續 chunk 不會再被寫進畫面。
      activeRootIdRef.current = rootId;
      // 不 abort 串流：後端在背景 task 生成，切走不影響它。
      // 舊的讀取迴圈會因為 activeRootIdRef 已改變而自行停止讀取。
      // 這裡只放掉 ref，讓新對話的送出流程不被舊串流綁住。
      streamAbortRef.current = null;
      setIsLoading(false);
      const res = await apiFetch(`/chats/${rootId}/history`);
      const data = await res.json();

      // 決定載入後要捲到哪：有存過的閱讀位置就還原，
      // 沒有的話（新開的對話 / 重新整理頁面後第一次載入）對齊最後一則 user 訊息
      const list = Array.isArray(data) ? data : [];
      const savedTop = readingPositions[positionKey];
      const lastUserId = [...list].reverse().find(m => m.role === 'user')?.id || null;
      setPendingRestore({
        convId: positionKey,
        scrollTop: typeof savedTop === 'number' ? savedTop : null,
        anchorId: typeof savedTop === 'number' ? null : lastUserId,
      });
      setRestoreToken(t => t + 1);

      setMessages(data); // 把舊對話填入畫面

      // 順便載入分支清單，讓已存在的錨點文字立刻亮起來
      loadBranchList(data?.[0]?.id);
      
      // 在手機版點擊後自動收起側邊欄 (優化體驗)
      if (window.innerWidth < 768) setSidebarOpen(false);

      // 最後一則是 AI 且內容還是空的 → 可能還在背景生成，改用續讀接回
      const last = list[list.length - 1];
      if (last && last.role === 'assistant' && !last.content && last.id) {
        resumeGeneration(rootId, last.id);
      }
    } catch (error) {
      console.error("載入對話失敗:", error);
    } finally {
      setIsLoading(false);
    }
  };

  // 續讀一則 AI 回覆：從 /messages/{id}/stream 讀取，逐段寫進畫面。
  // 只在「這串對話仍是目前顯示的對話」時寫入，切走就停止寫入。
  const resumeGeneration = async (rootId, messageId) => {
    try {
      const res = await apiFetch(`/messages/${messageId}/stream?offset=0`);
      if (!res.ok || !res.body) return;
      // 切走（activeRootIdRef 不等於 rootId）就停止讀取與寫入
      await readTextStream(res, {
        shouldStop: () => activeRootIdRef.current !== rootId,
        onText: (text) => setMessages(prev => {
          if (activeRootIdRef.current !== rootId) return prev;
          const next = [...prev];
          const lastMsg = next[next.length - 1];
          if (!lastMsg || lastMsg.id !== messageId) return prev;
          next[next.length - 1] = { ...lastMsg, content: text };
          return next;
        }),
      });
    } catch (error) {
      console.error("續讀失敗:", error);
    }
  };

  // 訊息渲染完成後才做還原／對齊，全部用瞬間定位（不做平滑動畫）。
  // 用 requestAnimationFrame 等版面穩定（Markdown、程式碼高亮會撐高高度）。
  useEffect(() => {
    if (!pendingRestore) return;
    const target = pendingRestore;
    let frame2 = null;
    const frame1 = requestAnimationFrame(() => {
      frame2 = requestAnimationFrame(() => {
        const container = scrollContainerRef.current;
        if (target.scrollTop != null) {
          if (container) container.scrollTop = target.scrollTop;
        } else if (target.anchorId) {
          scrollToMessage(target.anchorId, 'auto');
        } else if (container) {
          container.scrollTop = 0;
        }
        setPendingRestore(null);
      });
    });
    return () => {
      cancelAnimationFrame(frame1);
      if (frame2) cancelAnimationFrame(frame2);
    };
  }, [pendingRestore]);

  const startNewChat = () => {
    // 新對話沒有 root id，同時中止上一個對話還在跑的串流
    activeRootIdRef.current = null;
    // 不 abort：舊串流在背景繼續生成，切回去時用續讀接回
    streamAbortRef.current = null;
    setIsLoading(false);
    setMessages([]); // 清空畫面
    if (window.innerWidth < 768) setSidebarOpen(false);
  };

  // 發送訊息 (核心邏輯)
  const handleSend = async () => {
    if (!input.trim() || isLoading || !model) return;

    if (input.length > MAX_MESSAGE_CHARS) { alert(TOO_LONG_MSG); return; }
    const userMessageContent = input;
    setInput(""); // 馬上清空輸入框，提升體驗
    setIsLoading(true);

    // 計算 parent_id：畫面上最後一則就是父節點；畫面為空時為 null（新對話）。
    let parentId = null;
    if (messages.length > 0) {
      parentId = messages[messages.length - 1].id;
    }

    fetchHistory();

    // 樂觀加入 user 與空的 AI 佔位訊息（尚無 id，之後由回應 header 補上）
    setMessages(prev => [
      ...prev, 
      { role: 'user', content: userMessageContent },
      { role: 'assistant', content: '', model_used: model } // 提早顯示 Thinking
    ]);
    setSendScrollToken(t => t + 1); // 只有自己送出才會遞增

    fetchHistory();

    // 記下這則訊息所屬的對話（root id；新對話為 null）。串流期間若使用者切換對話，activeRootIdRef 會變，快照就不符。
    const sendConvId = messages[0]?.id ?? null;
    activeRootIdRef.current = sendConvId;
    if (streamAbortRef.current) {
      try { streamAbortRef.current.abort(); } catch { /* ignore */ }
    }
    const abortController = new AbortController();
    streamAbortRef.current = abortController;

    try {
      const response = await apiFetch(`/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: abortController.signal,
        body: JSON.stringify({
          message: userMessageContent,
          model: model,
          parent_id: parentId
        })
      });

      await throwIfQuotaError(response, user);
      if (!response.ok) throw new Error("API Error");
      
      if (messages.length === 0) {
        setTimeout(fetchHistory, 1000);
      }

      
      const newMsgId = response.headers.get("X-Message-Id");
      // 樂觀加入的 user 訊息沒有 id，後端在 X-User-Message-Id 回傳真正的 id；補到倒數第二則（沒有 id 的 user）。
      const newUserMsgId = response.headers.get("X-User-Message-Id");
      if (newUserMsgId && activeRootIdRef.current === sendConvId) {
        setMessages(prev => {
          if (activeRootIdRef.current !== sendConvId) return prev;
          const len = prev.length;
          if (len < 2) return prev;
          const last = prev[len - 1];
          const userMsg = prev[len - 2];
          if (last?.role !== 'assistant') return prev;
          if (!userMsg || userMsg.role !== 'user' || userMsg.id) return prev;
          const next = [...prev];
          next[len - 2] = { ...userMsg, id: newUserMsgId };
          return next;
        });
      }

      // 使用者已切到別的對話就停止套用（readTextStream 讀前、讀後各檢查一次）
      await readTextStream(response, {
        shouldStop: () => activeRootIdRef.current !== sendConvId,
        onText: (aiResponseText) => setMessages(prev => {
          // 對話已被切換就不寫入，避免污染新對話
          if (activeRootIdRef.current !== sendConvId) return prev;
          // 畫面已被清空就放棄更新
          if (prev.length === 0) return prev; 
          
          const newMessages = [...prev];
          const lastMsg = newMessages[newMessages.length - 1];
          
          // 確保最後一則訊息存在，且真的是 AI 的回覆
          if (!lastMsg || lastMsg.role !== 'assistant') return prev;

          lastMsg.content = aiResponseText;
          if (newMsgId) lastMsg.id = newMsgId;
          return newMessages;
        }),
      });

    } catch (error) {
      // 切換對話造成的 abort 不是錯誤，也不要再往畫面塞訊息
      if (error?.name === 'AbortError' || activeRootIdRef.current !== sendConvId) {
        // 已被切換的對話：靜默結束
      } else if (error instanceof QuotaError) {
        // 額度／速率限制：後端沒寫入，撤掉這次樂觀加入的 user + 空 AI 佔位，文字放回輸入框
        setMessages(prev => {
          const n = prev.length;
          const last = prev[n - 1];
          const userMsg = prev[n - 2];
          const trailingPair = n >= 2 && last?.role === 'assistant' && !last.id && !last.content
            && userMsg?.role === 'user' && !userMsg.id;
          return trailingPair ? prev.slice(0, n - 2) : prev;
        });
        setInput(userMessageContent);
        alert(error.message);
      } else {
        console.error("Error:", error);
        setMessages(prev => [...prev, { role: 'assistant', content: "⚠️ 連線錯誤，請檢查後端是否啟動。" }]);
      }
    } finally {
      // 只有還是「自己這條」串流時才清掉 loading，否則新對話自己的送出流程會被誤解除
      if (streamAbortRef.current === abortController) {
        streamAbortRef.current = null;
        setIsLoading(false);
      }
      refreshAfterGeneration();
    }
  };

  // 重新生成最後一則 AI 回覆（主對話）：原地清空該則、串流寫回同一個 message id
  // 後端會把舊回覆與其下的分支做「幽靈化」，成功後：重抓分支清單（高亮消失）、關掉被幽靈化的分支 tab
  // 第二參數 { userMsg, content }：編輯最後一則 user 訊息後重新生成（同一個函式，不另寫串流）
  //        content 有值 → 先把 userMsg 改成新內容，body 帶 content；後端失敗（被拒或沒送出）→ userMsg 還原成舊內容
  const handleRetry = async (msg, { userMsg, content } = {}) => {
    if (isLoading || !msg?.id || !model) return;
    if (content != null && content.length > MAX_MESSAGE_CHARS) { alert(TOO_LONG_MSG); return; }
    const retryId = msg.id;
    const editing = content != null && !!userMsg;
    const oldUser = userMsg?.content; // 先記下舊內容（失敗還原用），必須在任何 setState 之前
    // 額度拒絕時還原這則回覆的舊內容與舊模型，同樣必須在 setState 之前記下
    const oldContent = msg.content;
    const oldModel = msg.model_used;

    // 記下這串對話，切走就停止寫入
    const convId = messages[0]?.id ?? null;
    activeRootIdRef.current = convId;
    if (streamAbortRef.current) {
      try { streamAbortRef.current.abort(); } catch { /* ignore */ }
    }
    const abortController = new AbortController();
    streamAbortRef.current = abortController;
    setIsLoading(true);

    // 先清空這則回覆，讓使用者看到它重新生成；編輯時同時把 user 那則換成新內容（樂觀更新）
    setMessages(prev => prev.map(m => {
      if (editing && m.id === userMsg.id) return { ...m, content };
      return m.id === retryId ? { ...m, content: '', model_used: model } : m;
    }));

    // 伺服器是否已接受（response.ok）；還沒接受前失敗 → user 還原
    let accepted = false;
    try {
      const response = await apiFetch(`/messages/${retryId}/regenerate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: abortController.signal,
        body: JSON.stringify(content != null ? { model, content } : { model }),
      });

      await throwIfQuotaError(response, user);
      if (!response.ok) throw new Error("API Error");
      accepted = true;

      // 後端在 header 回傳這次被幽靈化的分支 id（逗號分隔，可能為空）
      const orphaned = (response.headers.get('X-Orphaned-Branch-Ids') || '').split(',').filter(Boolean);

      // 使用者已切換到別的對話 → 停止讀取，別把內容寫進別人的對話（readTextStream 讀前、讀後各檢查一次）
      const { finished } = await readTextStream(response, {
        shouldStop: () => activeRootIdRef.current !== convId,
        // 寫回「id 等於 retryId 的那則」，不是最後一則
        onText: (aiResponseText) => setMessages(prev => {
          if (activeRootIdRef.current !== convId) return prev;
          return prev.map(m => (m.id === retryId ? { ...m, content: aiResponseText } : m));
        }),
      });

      // 串流成功結束：分支高亮重抓；被幽靈化的分支 tab 關掉（若還是同一串對話才重抓清單）
      if (finished) {
        if (activeRootIdRef.current === convId) loadBranchList(convId);
        if (orphaned.length > 0) {
          closeBranches(orphaned);
        }
      }
    } catch (error) {
      // 請求被拒或根本沒送出（串流還沒開始）：資料庫沒改 → user 還原舊內容；串流中斷則保持新內容
      if (editing && !accepted && activeRootIdRef.current === convId) {
        setMessages(prev => prev.map(m => (m.id === userMsg.id ? { ...m, content: oldUser } : m)));
      }
      // 切換對話造成的 abort 不是錯誤，靜默結束
      if (error?.name === 'AbortError' || activeRootIdRef.current !== convId) {
        // 已被切換的對話：靜默結束
      } else if (error instanceof QuotaError) {
        // 額度／速率限制：後端沒寫入，把這則回覆還原成送出前的內容與模型
        setMessages(prev => prev.map(m => (
          m.id === retryId ? { ...m, content: oldContent, model_used: oldModel } : m
        )));
        alert(error.message);
      } else {
        console.error("重新生成失敗:", error);
        setMessages(prev => prev.map(m => (
          m.id === retryId ? { ...m, content: "⚠️ 連線錯誤，請檢查後端是否啟動。" } : m
        )));
      }
    } finally {
      // 只有還是「自己這條」串流時才清掉 loading
      if (streamAbortRef.current === abortController) {
        streamAbortRef.current = null;
        setIsLoading(false);
      }
      refreshAfterGeneration();
    }
  };

  return { input, setInput, messages, setMessages, isLoading, scrollContainerRef, restoreToken, sendScrollToken, loadChat, startNewChat, handleSend, handleRetry };
}
