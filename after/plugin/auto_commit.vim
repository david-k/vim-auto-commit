let s:python_bin = "python3"

let s:git_dir = g:vim_auto_commit_dir

let s:this_dir = expand('<sfile>:p:h')
let s:sync_script = s:this_dir ."/../../sync-repo.py"
let s:test_script = s:this_dir ."/../../test.py"


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
	let l:commit_msg = "[". g:vim_auto_commit_instance_name ."] auto-update: ". l:relative_name
	let l:cmd_git_commit = 'git commit -m '. shellescape(l:commit_msg)
	let l:cmd_git_add = 'git add '. shellescape(a:filename)

	call system(l:cmd_cd .' && '. l:cmd_git_add .' && '. l:cmd_git_commit)
	if v:shell_error != 0
		echoerr "Committing to git repo failed"
	endif

	call s:Push()
endfunction

let s:_prev_timer = -1
function! s:GitAutoCommit()
	if get(g:, "vim_auto_commit_enabled", 1) == 0
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

	let l:wait_time = get(g:, "vim_auto_commit_wait_time", 30000)  " 30s
	let s:_prev_timer = timer_start(l:wait_time, { _tid -> s:CommitCurrentFile(l:filename) })
endfunction


function! s:Push()
	let s:pull_job = job_start([s:python_bin, s:sync_script, "push", s:git_dir, g:vim_auto_commit_instance_name])
endfunction

function! s:Pull()
	let s:pull_job = job_start([s:python_bin, s:sync_script, "pull", s:git_dir, g:vim_auto_commit_instance_name])
endfunction


command! ACPush call s:Push()
command! ACPull call s:Pull()


augroup VimAutoCommit
	autocmd!
	autocmd BufWritePost * call s:GitAutoCommit()
augroup END

