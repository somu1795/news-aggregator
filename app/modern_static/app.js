document.addEventListener('DOMContentLoaded', () => {
    const headlinesContainer = document.getElementById('headlines-container');
    const themeToggle = document.getElementById('theme-toggle');

    // --- Theme Toggler ---
    const currentTheme = localStorage.getItem('theme') || 'light';
    document.documentElement.setAttribute('data-theme', currentTheme);
    if (currentTheme === 'dark') {
        themeToggle.textContent = '🌙';
    }

    themeToggle.addEventListener('click', () => {
        let theme = document.documentElement.getAttribute('data-theme');
        if (theme === 'dark') {
            theme = 'light';
            themeToggle.textContent = '☀️';
        } else {
            theme = 'dark';
            themeToggle.textContent = '🌙';
        }
        document.documentElement.setAttribute('data-theme', theme);
        localStorage.setItem('theme', theme);
    });

    // --- Data Fetching and Rendering ---
    const createHeadlineCard = (headline) => {
        const card = document.createElement('div');
        card.className = 'card';

        const title = document.createElement('h2');
        title.className = 'card-title';
        const link = document.createElement('a');
        try {
            const parsed = new URL(headline.link);
            if (parsed.protocol === 'https:' || parsed.protocol === 'http:') {
                link.href = headline.link;
            } else {
                link.href = '#';
            }
        } catch (e) {
            link.href = '#';
        }
        link.textContent = headline.title;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        title.appendChild(link);

        const meta = document.createElement('div');
        meta.className = 'card-meta';

        const source = document.createElement('span');
        source.className = 'card-source';
        source.textContent = headline.source;

        const publishedDate = new Date(headline.published * 1000);
        const time = document.createElement('time');
        time.setAttribute('datetime', publishedDate.toISOString());
        time.textContent = publishedDate.toLocaleDateString('en-US', {
            year: 'numeric', month: 'long', day: 'numeric'
        });

        meta.appendChild(source);
        meta.appendChild(time);
        card.appendChild(title);
        card.appendChild(meta);

        return card;
    };

    const renderError = (message) => {
        headlinesContainer.innerHTML = `<div class="error-message">${message}</div>`;
    };

    const fetchHeadlines = async () => {
        try {
            const response = await fetch('/api/headlines');
            if (!response.ok) {
                const errorData = await response.json().catch(() => ({ detail: 'An unknown server error occurred.' }));
                throw new Error(errorData.detail || `HTTP error! Status: ${response.status}`);
            }
            const apiResponse = await response.json();
            
            headlinesContainer.innerHTML = ''; // Clear loader/previous content

            if (apiResponse.data && apiResponse.data.length > 0) {
                apiResponse.data.forEach(headline => {
                    headlinesContainer.appendChild(createHeadlineCard(headline));
                });
            } else {
                renderError('No headlines are available at the moment.');
            }
        } catch (error) {
            console.error('Failed to fetch headlines:', error);
            renderError(`Could not load headlines: ${error.message}`);
        }
    };

    fetchHeadlines();
});