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

	call AutoCommitUpdateStatus()
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
	let l:command = [s:python_bin, s:sync_script, "push", s:git_dir, g:vim_auto_commit_instance_name]
	let s:pull_job = job_start(l:command, {"exit_cb": "s:OnCommandExit"})
endfunction

function! s:Pull()
	let l:command = [s:python_bin, s:sync_script, "pull", s:git_dir, g:vim_auto_commit_instance_name]
	let s:pull_job = job_start(l:command, {"exit_cb": "s:OnCommandExit"})
endfunction

function! s:OnCommandExit(job, exit_status)
	if a:exit_status != 0
		echohl ErrorMsg | echo "NoteSync push/pull command failed" | echohl None
	endif

	" Update statusline
	call AutoCommitUpdateStatus()
endfunction


let s:_upload_status = "none"

function! AutoCommitUpdateStatus()
	let l:dir = getcwd()
	if filereadable(l:dir . "/.notesync/latest_uploaded_commit")
		let l:uploaded_commit_id = readfile(l:dir . "/.notesync/latest_uploaded_commit", 1)[0]
		silent let l:current_commit_id = trim(system("git rev-parse master"))
		if l:uploaded_commit_id == l:current_commit_id
			let s:_upload_status = "pushed"
		else
			let s:_upload_status = "there are unpushed commits"
		endif

		redrawstatus
	else
		let s:_upload_status = "none"
	endif
endfunction

" This function is likely going to be called whenever the cursor moves, so it
" should be as efficient as possible so that it doesn't cause any stutter
function! AutoCommitStatusLine()
	if s:_upload_status == "none"
		return ""
	endif

	return "[Notes: ". s:_upload_status ."]"
endfunction


command! ACPush call s:Push()
command! ACPull call s:Pull()


augroup VimAutoCommit
	autocmd!
	autocmd BufWritePost * call s:GitAutoCommit()

	autocmd WinEnter * call AutoCommitUpdateStatus()
	autocmd FocusGained * call AutoCommitUpdateStatus()
	autocmd DirChanged * call AutoCommitUpdateStatus()
augroup END


