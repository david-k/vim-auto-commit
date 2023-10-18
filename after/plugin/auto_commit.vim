let s:git_dir = g:vim_auto_commit_dir
let s:enabled = get(g:, "vim_auto_commit_enabled", 1)
let s:wait_time = get(g:, "vim_auto_commit_wait_time", 30000)  " 30s


function! s:CommitCurrentFile(filename)
	" Don't auto-commit when editing files inside .git/ (like .git/COMMIT_EDITMSG)
	if stridx(a:filename, s:git_dir .'.git/') == 0
		return
	endif

	let l:cmd_cd = 'cd '. shellescape(s:git_dir)

	" `git diff --exit-code` succeeds if there are no changes, in which case
	" we return early
	call system(l:cmd_cd .' && git diff --exit-code '. shellescape(a:filename))
	if v:shell_error == 0
		return
	endif

	let l:relative_name = a:filename[strlen(s:git_dir) : ]
	let l:commit_msg = "auto: ". l:relative_name
	let l:cmd_git_commit = 'git commit -m '. shellescape(l:commit_msg)
	let l:cmd_git_add = 'git add '. shellescape(a:filename)

	call system(l:cmd_cd .' && '. l:cmd_git_add .' && '. l:cmd_git_commit)
	if v:shell_error != 0
		echoerr "Committing to git repo failed"
	endif
endfunction

let s:_prev_timer = -1
function! s:GitAutoCommit()
	if s:enabled == 0
		return
	endif

	let l:filename = expand('%:p')
	if stridx(l:filename, s:git_dir) != 0
		return
	endif

	if s:_prev_timer != -1
		" Stopping an already stopped timer is okay
		call timer_stop(s:_prev_timer)
	endif

	let s:_prev_timer = timer_start(s:wait_time, { _tid -> s:CommitCurrentFile(l:filename) })
endfunction

augroup VimAutoCommit
	autocmd!
	autocmd BufWritePost * call s:GitAutoCommit()
augroup END
