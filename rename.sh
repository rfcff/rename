#!/bin/bash
# create date 2022/06/08 gaoshiqiang
# 查找c源文件以及头文件函数  find -E . -regex '.*\.(c|h)' -type f -exec ctags -x --c-kinds=f {} ';'
# 查找cpp源文件函数        find -E . -regex '.*\.(cpp|h)' -type f -exec ctags -x --c-kinds=f {} ';'
# gaoshiqiang@gaoshiqiang jobase % ctags --list-kinds=c++
# c  classes
# d  macro definitions
# e  enumerators (values inside an enumeration)
# f  function definitions
# g  enumeration names
# l  local variables [off]
# m  class, struct, and union members
# n  namespaces
# p  function prototypes [off]
# s  structure names
# t  typedefs
# u  union names
# v  variable definitions
# x  external and forward variable declarations [off]
# 文件中 所有函数 信息 ctags -x --c-kinds=f + 文件名
# 文件中 全局变量 信息 ctags -x --c-kinds=v + 文件名
# 文件中 宏定义 信息 ctags -x --c-kinds=d + 文件名
# 查找文件struct/union/enum信息
# find -E . -regex '.*\.(cpp|c|h)' -type f -exec egrep "struct|enum|union" {} \;
# 当前目录下全部替换avcodec_register_all->temp_avcodec_register_all
# sed -i '' 's/\<avcodec_register_all\>/temp_avcodec_register_all/g' `egrep "\<avcodec_register_all\>" -rl .`
# macos下需要上面命令在脚本里有问题,用下面find脚本
# find -E . -regex '.*\.(cpp|c|h)' -type f | xargs egrep "\<${funcName}\>" -l | xargs gsed -i "s/\<$funcName\>/$dstFuncName/g"
# 字符串去重 sort -u funcs.txt 或者 uniq funcs.txt
# =======================================================================
# 通过处理jobase/transengine/thunder三个库重命名中遇到的问题，中介以下几点注意项
# 1.对外导出的接口类跟函数不改变名字，添加到exclude_dirs或者exclude_files中
# 2.调用了第三方库的类跟函数名不能改变名字
# 3.系统、语言接口需要添加到exclude_funcs中，不需要排除的目录中有函数跟需要排除的目录中重名的，需要添加到exclude_funcs
#   比如需要排除的文件中有函数名getTickCount，而需要修改的目录中同样有这个函数名，getTickCount就需要添加到exclude_funcs
# 4.exclude_funcs需要反复执行此脚本逐步确认，且是继承累加的，比如重命名a工程后得到exclude_funcs_a，
#   b库用到了a库，那么b的exclude_funcs就要包含exclude_funcs_a
# 5.部分类名跟文件名是一致的，在包含头文件的时候sed命令会统一替换，建议以添加前缀或者后缀的方式重命名，方便在处理所有文件之后
#   统一修复头文件名被修改的问题
# =======================================================================

# 查找ffmpeg函数
if [ ! -d "./temp" ]; then
  mkdir temp
fi
src_dirs=(liveengine)
list_files=(temp/funcs_thunder.txt)

# 要排除替换的目录
exclude_dirs=(liveengine/src/include
              liveengine/src/common
              liveengine/project
              liveengine/src/audio
              liveengine/src/video
              liveengine/src/service
            )
# 要排除替换的文件
exclude_files=(liveengine/src/LiveEngine.cpp
               liveengine/src/LiveEngine.h
               liveengine/src/LiveEngineEventCallBacker.cpp
               liveengine/src/LiveEngineEventCallBacker.h
               liveengine/src/trans/LiveEngineSmartDns.cpp
               liveengine/src/trans/LiveEngineSmartDns.h
              )
# 要排除替换的函数
exclude_funcs=(gettimeofday remove count strndup data c_str str send setup push_back pop
               set Set free Free size Size append Append lock Lock Width width Height height Insert insert Assert assert Parse parse Add add Reverse reverse
               run start stop finish init create deinit deInit destory increase decrease serialize front end begin parent cleanup clear destroy connect listen
               write read flush header zero empty limit open close update decode encode video audio in all reserve
               subtract member-body body isNull isArray asString getTickCount
               Convert Get Apply Sample Length Resolve Layout Extension File IFile User Delta Url Auth Utility Module Buffer
               Clear Optional Complete Host Packet Reset Release Mute Process Transport Probe Path Protocol
               calculate prepare maxsize global dump proxy onCall app subscribe infinite filtered onCallSync onTaskStart onTransStart
               Base64 base64 base64-encode base64-encoded
               isPublishAsStreamName getYYFlag setYYFlag getenv getenv_s GetEnv
               startXLogger stopXLogger addUser removeUser setLogLevel setLogFlags setLogCallback registerLogRpc triggerLogFileUpload putLog 
               setMaxQueueSize setJvm flushLogs setMaxFileNumLimit
               JologCreate JologRelease onRecvStringBroadCast onRecvRpcComplete
               Logger GetJVM JNIEnvPtr
               Java_com_jobase_JobaseNative_httpCallback httpCallback doHttpReqAsync HttpReqManagerApple
               JNI_OnLoad_Jobase JNI_OnUnLoad_Jobase
               JodnsGetInstance JodnsReleaseInstance doPreResolve getByName onNetworkChanged getCountryCode itemAt valueFor osalGetCacheDir
               TimeUtils AVframe getClientWanIP
               SetAndroidOpenSlEsParam EnableOpenSlEsOutputNewLogic SetAudioAdaptationConfig SetAudioKaraokeCompValue SetAndroidAudioLayer DisableAdaptSystemKaraoke
               IsSupportCodec onArgoQuery getLiveResolutionConfigInfo
               )

rm -f temp/funcs_name.txt
rm -f temp/funcs_name_uniq.txt

# 全局变量相关文件
rm -f temp/global_var_with_file.txt
rm -f temp/global_var.txt
rm -f temp/global_var_uniq.txt

# 宏相关文件
rm -f temp/macro_def_with_file.txt
rm -f temp/macro_def.txt
rm -f temp/macro_def_uniq.txt

for file in ${list_files[*]}
do
  rm -f ${file}
  echo "rm -f ${file}"
done


# funcs_cpp_file=${list_files[3]}
# echo "cpp funcs file $funcs_cpp_file"
# ctags -x --c++-kinds=f libavcodec/crypto.cpp > temp/$funcs_cpp_file
# ctags -x --c++-kinds=v libavcodec/crypto.cpp > temp/global_var_with_file.txt
# ctags -x --c++-kinds=d libavcodec/crypto.cpp > temp/macro_def_with_file.txt
# excludr_dir_str="\("
# len=${#exclude_dirs[*]}
# echo "len:${len}, `expr ${len} - 1`"
for((idx=0; idx<${#exclude_dirs[*]}; idx++))
do
  echo "exclude dir: ${exclude_dirs[idx]}, ${idx}"
  # max=`expr ${len} - 1`
  excludr_dir_str="${excludr_dir_str} -path \"${exclude_dirs[idx]}\" -prune -o"
  # if (( ${idx} < ${max} )) ;then
  #   excludr_dir_str="${excludr_dir_str} -prune -o"
  # fi
done
# excludr_dir_str="${excludr_dir_str} \)"
echo "exclude dir str: ${excludr_dir_str}"

# 找出排除目录列表中的函数与类
rm -f temp/funcs_exclude.txt
rm -f temp/funcs_exclude_name.txt
rm -f temp/funcs_exclude_uniq.txt
for((idx=0; idx<${#exclude_dirs[*]}; idx++))
do
  echo "exclude_dirs ${exclude_dirs[idx]}"
  find -E ${exclude_dirs[idx]} -regex '.*\.(cpp|cc|cxx|h)' -type f -exec ctags -x --c++-kinds=fc {} ';' >> temp/funcs_exclude.txt
done
# 找出排除文件列表中的函数与类
for((idx=0; idx<${#exclude_files[*]}; idx++))
do
  echo "exclude_files ${exclude_files[idx]}"
  ctags -x --c++-kinds=fc ${exclude_files[idx]} >> temp/funcs_exclude.txt
done

for((idx=0; idx<${#src_dirs[*]}; idx++))
do
  echo "src_dirs ${src_dirs[idx]}"
  # find -E src  -path "src/jolog/common" -prune -o -path "src/json" -prune -o -regex '.*\.(cpp|cc|cxx|h)' -type f -exec ctags -x --c++-kinds=fc {} ';' >  temp/funcs_jobase.txt
  # echo "find -E ${src_dirs[idx]} ${excludr_dir_str} -regex '.*\.(cpp|cc|cxx|h)' -type f -exec ctags -x --c++-kinds=fc {} ';' >  $funcs_file"
  find -E ${src_dirs[idx]} ${excludr_dir_str} -regex '.*\.(cpp|cc|cxx|h)' -type f -exec ctags -x --c++-kinds=fc {} ';' >>  temp/funcs_thunder.txt
  # find -E ${src_dirs[idx]} -regex '.*\.(cpp|cc|cxx|h)' -type f -exec ctags -x --c++-kinds=v {} ';' >> temp/global_var_with_file.txt
  # find -E ${src_dirs[idx]} -regex '.*\.(cpp|cc|cxx|h)' -type f -exec ctags -x --c++-kinds=d {} ';' >> temp/macro_def_with_file.txt
done

echo "finish find classes, functions, global vars, macros!!!!!!!!!!!"

# merge function names
for file in ${list_files[*]}
do
  while read line
  do
    echo $line|awk -F " " '{print $1}' >> temp/funcs_name.txt
  done < $file
done

# figure out exclude function names
while read line
do
  echo $line|awk -F " " '{print $1}' >> temp/funcs_exclude_name.txt
done < temp/funcs_exclude.txt

# # merge global vars
# while read globalv
# do
#   echo $globalv|awk -F " " '{print $1}' >> temp/global_var.txt
# done < temp/global_var_with_file.txt

# # merge macro defs
# while read macrod
# do
#   echo $macrod|awk -F " " '{print $1}' >> temp/macro_def.txt
# done < temp/macro_def_with_file.txt


# 字符串去重
sort -u temp/funcs_name.txt > temp/funcs_name_uniq.txt
# sort -u temp/global_var.txt > temp/global_var_uniq.txt
# sort -u temp/macro_def.txt  > temp/macro_def_uniq.txt
sort -u temp/funcs_exclude_name.txt > temp/funcs_exclude_uniq.txt
echo "finish uniq classes, functions, global vars, macros!!!!!!!!!!!"

# 删除 temp/funcs_name_uniq.txt 中的 exclude_funcs 函数与类
for((idx=0; idx<${#exclude_funcs[*]}; idx++))
do
  funcs_name=${exclude_funcs[idx]}
  echo "remove funcs ${funcs_name} in temp/funcs_name_uniq.txt"
  # 替换 gsed -i 's/\<operator\>//g' temp/funcs_name_uniq.txt
  # 删除包含字符的行 gsed -i "/\<HttpClient\>/d" temp/funcs_name_uniq.txt
  gsed -i "/\<$funcs_name\>/d" temp/funcs_name_uniq.txt
done
# 删除 temp/funcs_name_uniq.txt 中的 temp/funcs_exclude_uniq.txt 函数与类
while read funcs_name
do
  echo "remove funcs ${funcs_name} in temp/funcs_name_uniq.txt"
  # 删除包含字符的行 gsed -i "/\<HttpClient\>/d" temp/funcs_name_uniq.txt
  gsed -i "/\<$funcs_name\>/d" temp/funcs_name_uniq.txt
done < temp/funcs_exclude_uniq.txt

# 全局替换 temp/funcs_name_uniq.txt中的函数
while read funcName
do
  if [ -z $funcName ]; then
    echo "funcs_name_uniq $funcName empty need not rename"
    continue
  fi
  if [[ $funcName = ~* ]]; then
    echo "funcs_name_uniq $funcName destructor need not rename"
    continue
  fi

  dstFuncName="temp_${funcName}"
  echo "rename $funcName --> $dstFuncName"
  # 严格区分大小写,全word匹配
  for((idx=0; idx<${#src_dirs[*]}; idx++))
  do
    # echo "find -E ${src_dirs[idx]} ${excludr_dir_str} -regex '.*\.(cpp|cc|cxx|h)' -type f | xargs egrep \"\<${funcName}\>\" -l | xargs gsed -i \"s/\<$funcName\>/$dstFuncName/g\""
    find -E ${src_dirs[idx]} ${excludr_dir_str} -regex '.*\.(cpp|cc|cxx|h)' -type f | xargs egrep "\<${funcName}\>" -l | xargs gsed -i "s/\<$funcName\>/$dstFuncName/g"
  done
  # sed -i '' 's/$funcName/$dstFuncName/g' `egrep "${funcName}" -rl .`
  # echo "sed -i '' 's/$funcName/$dstFuncName/g' #egrep \"${funcName}\" -rl .#" >> ../sed_funcs_name_uniq1.txt
  # break
done < temp/funcs_name_uniq.txt

# # # 全局替换 temp/f_struct_uniq.txt中结构体、枚举、联合体
# while read name
# do
#   dstName="temp_${name}"
#   echo "rename $name --> $dstName"
#   # 严格区分大小写,全word匹配
#   for((idx=0; idx<${#src_dirs[*]}; idx++))
#   do
#     find -E ${src_dirs[idx]} ${excludr_dir_str} -regex '.*\.(cpp|cc|cxx|h)' -type f | xargs egrep "\<${name}\>" -l | xargs gsed -i "s/\<$name\>/$dstName/g"
#   done
#   # ctags --regex-c++=/\<$name\>/$dstName
# done < temp/f_struct_uniq.txt

# 全局替换 temp/global_var_uniq.txt中全局变量
# while read name
# do
#   dstName="temp_${name}"
#   echo "rename $name --> $dstName"
#   # 严格区分大小写,全word匹配
#   find -E . -regex '.*\.(cpp|cc|cxx|h)' -type f | xargs egrep "\<${name}\>" -l | xargs gsed -i "s/\<$name\>/$dstName/g"
# done < temp/global_var_uniq.txt

# # 全局替换 temp/macro_def_uniq.txt中全局变量
# while read name
# do
#   dstName="temp_${name}"
#   echo "rename $name --> $dstName"
#   # 严格区分大小写,全word匹配
#   find -E . -regex '.*\.(cpp|cc|cxx|h)' -type f | xargs egrep "\<${name}\>" -l | xargs gsed -i "s/\<$name\>/$dstName/g"
# done < temp/macro_def_uniq.txt

# 全局替换src目录下被改动的头文件以及TAG
for((idx=0; idx<${#src_dirs[*]}; idx++))
do
  # 严格区分大小写,全word匹配
  # echo "find -E ${src_dirs[idx]} ${excludr_dir_str} -regex '.*\.(cpp|cc|cxx|h)' -type f | xargs egrep \"\<${funcName}\>\" -l | xargs gsed -i \"s/\<$funcName\>/$dstFuncName/g\""
  find -E ${src_dirs[idx]} -regex '.*\.(cpp|cc|cxx|h)' -type f | xargs egrep "#define TAG \"temp_" -l | xargs gsed -i "s/#define TAG \"temp_/#define TAG \"/g"
  find -E ${src_dirs[idx]} -regex '.*\.(cpp|cc|cxx|h)' -type f | xargs egrep "#include \"temp_" -l | xargs gsed -i "s/#include \"temp_/#include \"/g"
  find -E ${src_dirs[idx]} -regex '.*\.(cpp|cc|cxx|h)' -type f | xargs egrep "#include <temp_" -l | xargs gsed -i "s/#include <temp_/#include </g"
  # 替换头文件中的 /temp_ 为 /
  find -E ${src_dirs[idx]} -regex '.*\.(cpp|cc|cxx|h)' -type f | xargs egrep "\/temp_" -l | xargs gsed -i "s/\/temp_/\//g"
done
# 回退日志tag的改动
# git checkout ../src/common/log/LogTypes.h

echo "!!!!!!!!!!!!!!!!!!!!!!!!!done rename finish!!!!!!!!!!!!!!!!!!!!!!!!!"

