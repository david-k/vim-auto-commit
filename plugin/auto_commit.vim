let s:python_bin = "python3"

let s:this_dir = expand('<sfile>:p:h')
let s:sync_script = s:this_dir ."/../sync-repo.py"
let s:_upload_info_filename = ""

let s:_prev_timer = -1
let s:_upload_status = "none"
let s:_commit_pending = v:false
let s:_push_in_progress = v:false


function! s:HasUncommitedChanges()
	let l:git_dir = shellescape(g:vim_auto_commit_dir)
	" `git diff --exit-code` succeeds if there are no changes, in which case
	" we return early
	call system('cd '. l:git_dir .' && git diff --exit-code && git diff --cached --exit-code')
	return v:shell_error != 0
endfunction

function! s:CommitCurrentFile()
	if !s:HasUncommitedChanges()
		return
	endif

	let l:commit_msg = "[". g:vim_auto_commit_instance_name ."] auto-commit"
	let l:cmd_git_commit = 'git commit -m '. shellescape(l:commit_msg)

	let l:cmd_cd = 'cd '. shellescape(g:vim_auto_commit_dir)
	call system(l:cmd_cd .' && git add -u && '. l:cmd_git_commit)
	if v:shell_error != 0
		echoerr "Committing to git repo failed"
	endif

	let s:_commit_pending = v:false
	call s:AutoCommitUpdateStatus()
	call s:Push()
endfunction

function! s:GitAutoCommit()
	if get(g:, "vim_auto_commit_enabled", 1) == 0
		return
	endif

	let l:filename = expand('%:p')
	if stridx(l:filename, g:vim_auto_commit_dir) != 0
		return
	endif

	if s:_prev_timer != -1
		" Stopping an already stopped timer is okay
		call timer_stop(s:_prev_timer)
	endif

	let s:_commit_pending = v:true
	call s:AutoCommitUpdateStatus()

	let l:wait_time = get(g:, "vim_auto_commit_wait_time", 30000)  " 30s
	let s:_prev_timer = timer_start(l:wait_time, { _tid -> s:CommitCurrentFile() })
endfunction

function! s:Push()
	let l:command = [s:python_bin, s:sync_script, "push", g:vim_auto_commit_dir, g:vim_auto_commit_instance_name]
	let s:pull_job = job_start(l:command, {"exit_cb": "s:OnCommandExit"})

	let s:_push_in_progress = v:true
	call s:AutoCommitUpdateStatus()
endfunction

function! s:Pull()
	let l:command = [s:python_bin, s:sync_script, "pull", g:vim_auto_commit_dir, g:vim_auto_commit_instance_name]
	let s:pull_job = job_start(l:command, {"exit_cb": "s:OnCommandExit"})
endfunction

function! s:OnCommandExit(job, exit_status)
	let s:_push_in_progress = v:false
	call s:AutoCommitUpdateStatus()

	if a:exit_status != 0
		echohl ErrorMsg | echo "NoteSync push/pull command failed" | echohl None
	endif

	" Update statusline
	call s:AutoCommitUpdateStatus()
endfunction

function! s:OnDirChange()
	let l:dir = getcwd()
	if filereadable(l:dir . "/.notesync/latest_upload_info")
		let s:_upload_info_filename = l:dir . "/.notesync/latest_upload_info"
	else
		let s:_upload_info_filename = ""
	endif

	call s:AutoCommitUpdateStatus()
endfunction

function! s:AutoCommitUpdateStatus()
	if s:_upload_info_filename != ""
		let l:upload_info = json_decode(join(readfile(s:_upload_info_filename), "\n"))
		let l:uploaded_commit_id = l:upload_info["included_commit_id"]
		silent let l:current_commit_id = trim(system("git rev-parse master"))
		if s:_commit_pending
			let s:_upload_status = "*"
		elseif s:_push_in_progress
			let s:_upload_status = "…"
		elseif s:HasUncommitedChanges()
			let s:_upload_status = "~"
		elseif l:uploaded_commit_id == l:current_commit_id
			let s:_upload_status = "✔" " everything is pushed
		endif

		redrawstatus!
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
	autocmd DirChanged * call s:OnDirChange()
	autocmd FocusGained * call s:AutoCommitUpdateStatus()
augroup END
