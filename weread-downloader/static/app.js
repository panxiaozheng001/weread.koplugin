function app() {
  return {
    // State
    view: 'login',     // login | shelf | detail | progress
    loggedIn: false,
    accountName: '',
    loading: false,

    // Login
    qrImageUrl: '',
    confirmUrl: '',
    loginStatus: '',     // pending | awaiting_otp | confirmed | error
    loginError: '',
    otpInput: '',
    otpError: '',
    pollTimer: null,

    // Shelf
    books: [],
    displayedBooks: [],
    searchQuery: '',

    // Book detail
    selectedBook: null,
    chapters: [],
    selectedChapters: [],
    psvts: '',

    // Download
    currentTask: null,
    taskId: '',
    epubFilename: '',
    eventSource: null,

    async init() {
      const resp = await this.api('/api/auth/status');
      this.loggedIn = resp.logged_in;
      this.accountName = resp.account_name || '';
      if (this.loggedIn) {
        this.view = 'shelf';
        this.loadShelf();
      }
    },

    // ---- API helper ----
    async api(url, opts = {}) {
      const resp = await fetch(url, {
        headers: { 'Content-Type': 'application/json' },
        ...opts,
      });
      return resp.json();
    },

    // ---- Login ----
    async startLogin() {
      this.loginError = '';
      this.qrImageUrl = '';
      try {
        const data = await this.api('/api/auth/qr');
        this.qrImageUrl = data.qr_image_url;
        this.confirmUrl = data.confirm_url;
        this.loginStatus = 'pending';
        this.startPolling();
      } catch (e) {
        this.loginError = '获取二维码失败: ' + e.message;
      }
    },

    startPolling() {
      if (this.pollTimer) clearInterval(this.pollTimer);
      this.pollTimer = setInterval(async () => {
        if (this.loginStatus === 'confirmed' || this.loginStatus === 'error') {
          clearInterval(this.pollTimer);
          return;
        }
        try {
          const data = await this.api('/api/auth/qr/status');
          if (data.status === 'confirmed') {
            this.loginStatus = 'confirmed';
            this.loggedIn = true;
            this.accountName = data.account_name || '';
            this.view = 'shelf';
            this.loadShelf();
            clearInterval(this.pollTimer);
          } else if (data.status === 'awaiting_otp') {
            this.loginStatus = 'awaiting_otp';
          } else if (data.status === 'otp_not_match') {
            this.otpError = '验证码错误，请重新输入';
          } else if (data.status === 'error' || data.status === 'timeout') {
            this.loginError = data.error || '登录失败';
            this.loginStatus = 'error';
            clearInterval(this.pollTimer);
          }
        } catch (e) {
          // keep polling
        }
      }, 3000);
    },

    async submitOtp() {
      this.otpError = '';
      const otp = this.otpInput.trim();
      if (!otp || otp.length !== 4) {
        this.otpError = '请输入4位验证码';
        return;
      }
      try {
        const data = await this.api('/api/auth/qr/status?otp=' + encodeURIComponent(otp));
        if (data.status === 'confirmed') {
          this.loginStatus = 'confirmed';
          this.loggedIn = true;
          this.accountName = data.account_name || '';
          this.view = 'shelf';
          this.loadShelf();
          clearInterval(this.pollTimer);
        } else if (data.status === 'otp_not_match') {
          this.otpError = '验证码错误';
        } else if (data.status === 'awaiting_otp') {
          this.otpError = '验证码错误，请重试';
        }
      } catch (e) {
        this.otpError = '请求失败: ' + e.message;
      }
    },

    // ---- Shelf ----
    async loadShelf() {
      this.loading = true;
      this.view = 'shelf';
      try {
        const data = await this.api('/api/shelf');
        this.books = data.books || [];
        this.displayedBooks = this.books;
        this.searchQuery = '';
      } catch (e) {
        this.books = [];
        this.displayedBooks = [];
      }
      this.loading = false;
    },

    async searchBooks() {
      const q = this.searchQuery.trim();
      if (!q) {
        this.displayedBooks = this.books;
        return;
      }
      this.loading = true;
      try {
        const data = await this.api('/api/search?q=' + encodeURIComponent(q));
        this.displayedBooks = data.books || [];
      } catch (e) {
        this.displayedBooks = [];
      }
      this.loading = false;
    },

    // ---- Book detail ----
    async selectBook(book) {
      this.selectedBook = book;
      this.selectedChapters = [];
      this.chapters = [];
      this.view = 'detail';
      this.loading = true;
      const bookId = book.book_id || book.bookId;
      try {
        const data = await this.api('/api/book/' + bookId + '/chapters');
        this.chapters = data.chapters || [];
        this.psvts = data.psvts || '';
      } catch (e) {
        this.chapters = [];
      }
      this.loading = false;
    },

    toggleChapter(uid, checked) {
      if (checked) {
        if (!this.selectedChapters.includes(uid)) {
          this.selectedChapters.push(uid);
        }
      } else {
        this.selectedChapters = this.selectedChapters.filter(u => u !== uid);
      }
    },

    selectAllChapters() {
      if (this.selectedChapters.length === this.chapters.length) {
        this.selectedChapters = [];
      } else {
        this.selectedChapters = this.chapters.map(ch => ch.chapterUid);
      }
    },

    // ---- Download ----
    async downloadSelected() {
      if (this.selectedChapters.length === 0) return;
      const bookId = this.selectedBook.book_id || this.selectedBook.bookId;
      const selected = this.chapters.filter(ch =>
        this.selectedChapters.includes(ch.chapterUid)
      );
      try {
        const data = await this.api('/api/download/chapters', {
          method: 'POST',
          body: JSON.stringify({
            book_id: bookId,
            title: this.selectedBook.title,
            chapters: selected,
          }),
        });
        this.taskId = data.task_id;
        this.view = 'progress';
        this.currentTask = { ...data, book_title: this.selectedBook.title };
        this.connectSSE(data.task_id);
      } catch (e) {
        alert('下载启动失败: ' + e.message);
      }
    },

    connectSSE(taskId) {
      if (this.eventSource) this.eventSource.close();
      this.eventSource = new EventSource('/api/download/' + taskId + '/events');
      this.eventSource.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          this.currentTask = data;
          if (data.status === 'completed') {
            this.epubFilename = (data.epub_path || '').split(/[/\\]/).pop();
            this.eventSource.close();
          }
          if (data.status === 'failed' || data.status === 'cancelled') {
            this.eventSource.close();
          }
        } catch (e) {}
      };
      this.eventSource.onerror = () => {
        this.eventSource.close();
      };
    },

    async cancelDownload() {
      if (!this.taskId) return;
      await this.api('/api/download/' + this.taskId + '/cancel', { method: 'POST' });
    },
  };
}
